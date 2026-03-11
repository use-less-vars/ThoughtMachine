import difflib
import re
from pathlib import Path
from typing import List, Dict, Union, Optional, Tuple
from pydantic import Field
from .base import ToolBase


class _ApplyEditsEngine:
    """Apply search/replace edits to file(s) with resilient matching. Edits are applied sequentially; if any find fails, the file is unchanged."""

    def __init__(self, file_path: Union[str, Path], edits: List[Dict], use_regex: bool = False, preview: bool = False):
        self.file_path = Path(file_path)
        self.edits = edits
        self.use_regex = use_regex
        self.preview = preview
        self.original_content = None
        self.modified_content = None

    def _normalize_lines(self, text: str) -> List[str]:
        """Split into lines, strip trailing whitespace, keep leading indentation."""
        lines = text.splitlines()
        # Strip trailing whitespace from each line, but keep leading spaces/tabs
        return [line.rstrip() for line in lines]

    def _find_normalized_block(self, content: str, find_text: str) -> List[Tuple[int, int]]:
        """
        Find all start/end positions of blocks that match find_text after normalization.
        Returns list of (start_index, end_index) in the original content string.
        """
        # Normalize content and find text
        content_lines = content.splitlines(keepends=True)
        norm_content_lines = self._normalize_lines(content)
        norm_find_lines = self._normalize_lines(find_text)
        find_len = len(norm_find_lines)

        matches = []
        # Slide over content lines
        for i in range(len(norm_content_lines) - find_len + 1):
            if norm_content_lines[i:i+find_len] == norm_find_lines:
                # Compute start and end indices in original content
                start = sum(len(line) for line in content_lines[:i])
                end = start + sum(len(line) for line in content_lines[i:i+find_len])
                matches.append((start, end))
        return matches
    def _apply_single_edit_regex(self, content: str, edit: Dict, edit_index: int) -> str:
        find_text = edit.get("find")
        replace_text = edit.get("replace", "")
        occurrence = edit.get("occurrence", 1)

        if not isinstance(find_text, str):
            raise ValueError(f"Edit {edit_index}: 'find' must be a string.")

        # Find all matches
        matches = list(re.finditer(find_text, content))
        if not matches:
            raise ValueError(
                f"Edit {edit_index}: Could not find regex pattern in content."
            )

        if occurrence == 0:
            # Replace all matches
            return re.sub(find_text, replace_text, content)
        else:
            if occurrence < 1 or occurrence > len(matches):
                raise ValueError(
                    f"Edit {edit_index}: Occurrence {occurrence} requested but only "
                    f"{len(matches)} regex matches found."
                )
            # Replace the nth match
            match = matches[occurrence - 1]
            start, end = match.start(), match.end()
            return content[:start] + replace_text + content[end:]

    def _apply_single_edit(self, content: str, edit: Dict, edit_index: int) -> str:
        find_text = edit.get("find")
        replace_text = edit.get("replace", "")
        occurrence = edit.get("occurrence", 1)

        if not isinstance(find_text, str):
            raise ValueError(f"Edit {edit_index}: 'find' must be a string.")

        if self.use_regex:
            return self._apply_single_edit_regex(content, edit, edit_index)
        
        # First try exact match (fast and unambiguous)
        exact_matches = []
        start = 0
        while True:
            pos = content.find(find_text, start)
            if pos == -1:
                break
            exact_matches.append(pos)
            start = pos + len(find_text)

        if exact_matches:
            # Use exact matches if they exist
            if occurrence == 0:
                return content.replace(find_text, replace_text)
            else:
                if occurrence < 1 or occurrence > len(exact_matches):
                    raise ValueError(
                        f"Edit {edit_index}: Occurrence {occurrence} requested but only "
                        f"{len(exact_matches)} exact matches found."
                    )
                pos = exact_matches[occurrence - 1]
                return content[:pos] + replace_text + content[pos + len(find_text):]

        # If no exact match, try normalized matching
        matches = self._find_normalized_block(content, find_text)
        if not matches:
            # Provide helpful error with closest normalized block
            norm_find_lines = self._normalize_lines(find_text)
            content_lines = content.splitlines()
            norm_content_lines = self._normalize_lines(content)
            matcher = difflib.SequenceMatcher(None, norm_content_lines, norm_find_lines)
            blocks = matcher.get_matching_blocks()
            if blocks and blocks[0].size > 0:
                suggestion_line = blocks[0].a  # line index in content where match starts
                start_line = max(0, suggestion_line - 2)
                end_line = min(len(content_lines), suggestion_line + 5)
                context = "\n".join(content_lines[start_line:end_line])
                raise ValueError(
                    f"Edit {edit_index}: Could not find normalized block. "
                    f"Closest match starts around line {suggestion_line+1}:\n"
                    f"{context}\n"
                    "Check for indentation or content differences."
                )
            else:
                raise ValueError(
                    f"Edit {edit_index}: Could not find block matching (normalized):\n"
                    f"{find_text[:200]}..."
                )

        # Handle normalized matches
        if occurrence == 0:
            # Replace all normalized matches
            # Work backwards to preserve indices
            new_content = content
            for start, end in reversed(matches):
                new_content = new_content[:start] + replace_text + new_content[end:]
            return new_content
        else:
            if occurrence < 1 or occurrence > len(matches):
                raise ValueError(
                    f"Edit {edit_index}: Occurrence {occurrence} requested but only "
                    f"{len(matches)} normalized matches found."
                )
            start, end = matches[occurrence - 1]
            return content[:start] + replace_text + content[end:]

    def run(self) -> Dict:
        """Execute all edits. Returns {'success': bool, 'message': str, 'diff': str?}."""
        try:
            if not self.file_path.exists():
                return {
                    "success": False,
                    "message": f"File not found: {self.file_path}"
                }

            # Read file with utf-8 encoding
            self.original_content = self.file_path.read_text(encoding="utf-8")
            self.modified_content = self.original_content

            # Apply each edit sequentially
            for i, edit in enumerate(self.edits, start=1):
                self.modified_content = self._apply_single_edit(
                    self.modified_content, edit, edit_index=i
                )

            # Write only if all edits succeeded
            if self.modified_content != self.original_content:
                diff = self._generate_diff()
                if not self.preview:
                    self.file_path.write_text(self.modified_content, encoding="utf-8")
                    return {
                        "success": True,
                        "message": f"Successfully applied {len(self.edits)} edit(s).",
                        "diff": diff
                    }
                else:
                    return {
                        "success": True,
                        "message": f"Preview mode: {len(self.edits)} edit(s) would be applied.",
                        "diff": diff
                    }
            else:
                return {
                    "success": True,
                    "message": "No changes were made (edits matched but resulted in identical content)."
                }

        except Exception as e:
            return {
                "success": False,
                "message": f"Error: {str(e)}"
            }

    def _generate_diff(self) -> str:
        """Generate a unified diff of the changes."""
        original_lines = self.original_content.splitlines(keepends=True)
        modified_lines = self.modified_content.splitlines(keepends=True)
        diff = difflib.unified_diff(
            original_lines, modified_lines,
            fromfile=str(self.file_path),
            tofile=str(self.file_path),
            lineterm=""
        )
        return "".join(diff)


class ApplyEdits(ToolBase):
    """Apply search/replace edits to file(s) with resilient matching. Edits are applied sequentially; if any find fails, the file is unchanged."""
    file_path: Optional[str] = Field(None, description="Path to the file to edit (optional if files or file_pattern provided)")
    edits: List[dict] = Field(description="List of edit dictionaries. Each edit must have 'find' and 'replace' keys, and optionally 'occurrence' (default 1, 0 for all).")
    files: Optional[List[str]] = Field(None, description="List of file paths to edit (optional if file_path or file_pattern provided)")
    file_pattern: Optional[str] = Field(None, description="Glob pattern to match files (optional if file_path or files provided)")
    preview: bool = Field(False, description="If True, only show diff without writing")
    use_regex: bool = Field(False, description="If True, treat find as regex pattern")

    def _get_file_paths(self) -> List[Path]:
        '''Return list of Path objects for files to edit based on file_path, files, or file_pattern.'''
        import os
        if self.files:
            validated_paths = []
            for f in self.files:
                try:
                    validated = self._validate_path(f)
                    validated_paths.append(Path(validated))
                except ValueError as e:
                    raise ValueError(f"File '{f}' is outside workspace: {e}")
            return validated_paths
        elif self.file_pattern:
            # Use glob recursively if pattern contains **
            pattern = self.file_pattern
            if '**' in pattern:
                raw_paths = list(Path('.').glob(pattern))
            else:
                raw_paths = list(Path('.').glob(pattern))
            validated_paths = []
            for p in raw_paths:
                try:
                    validated = self._validate_path(str(p))
                    validated_paths.append(Path(validated))
                except ValueError:
                    # Skip files outside workspace
                    continue
            return validated_paths
        elif self.file_path:
            try:
                validated = self._validate_path(self.file_path)
                return [Path(validated)]
            except ValueError as e:
                raise ValueError(f"File '{self.file_path}' is outside workspace: {e}")
        else:
            raise ValueError('Must provide one of file_path, files, or file_pattern.')
    
    def execute(self) -> str:
        """Execute the edits and return a human-readable result."""
        from pathlib import Path
        
        try:
            file_paths = self._get_file_paths()
        except ValueError as e:
            return f"Error: {e}"
        
        results = []
        diffs = []
        for file_path in file_paths:
            engine = _ApplyEditsEngine(
                file_path, self.edits,
                use_regex=self.use_regex,
                preview=self.preview
            )
            result = engine.run()
            if result["success"]:
                results.append(f"{file_path}: {result['message']}")
                if result.get("diff"):
                    diffs.append(f"--- {file_path}\n{result['diff']}")
            else:
                results.append(f"{file_path}: ERROR - {result['message']}")
        
        summary = f"Processed {len(file_paths)} file(s).\n" + "\n".join(results)
        if diffs:
            summary += "\n\nDiffs:\n" + "\n".join(diffs)
        return summary


