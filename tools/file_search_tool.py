from .base import ToolBase
from .utils import DEFAULT_EXCLUDE_DIRS
import os
import re
import fnmatch
import bisect
from pathlib import Path
from pydantic import Field
from typing import List, Optional, ClassVar, Literal

class FileSearchTool(ToolBase):
    required_categories: ClassVar[List[str]] = ["filesystem:read"]
    """Search for patterns across multiple files or directories with regex, multiline, context lines, and line numbers.
    Supports regex (with (?s) flag for dot-matches-newline) and plain text multi-line searches.
    Use file_pattern glob to limit files, or directory/filenames."""
    tool: Literal["FileSearchTool"] = "FileSearchTool"
    
    # Safety limits
    MAX_FILE_SIZE: ClassVar[int] = 10_000_000  # 10MB
    MAX_LINES_PER_FILE: ClassVar[int] = 100_000
    MAX_CONTEXT_LINES: ClassVar[int] = 20
    MAX_OUTPUT_CHARS: ClassVar[int] = 40_000
    MAX_RESULTS: ClassVar[int] = 200
    MAX_FILES_TO_SEARCH: ClassVar[int] = 1000

    # Per-file cap on printed match blocks. Matches beyond the cap are NOT
    # silently dropped: one notice line names the count and their line numbers.
    MAX_MATCHES_PER_FILE: ClassVar[int] = 5
    # How many omitted line numbers the notice lists before eliding with '...'.
    MAX_OMITTED_LINES_SHOWN: ClassVar[int] = 20
    MORE_MATCHES_NOTICE: ClassVar[str] = "[+{count} more matches in this file at lines {lines}]"
    CHAR_BUDGET_WARNING: ClassVar[str] = (
        "[Output truncated by the {budget} character budget: {dropped} further "
        "matches dropped. Narrow the pattern/scope or paginate to see them.]"
    )
    CONTEXT_DROPPED_WARNING: ClassVar[str] = (
        "[Context lines dropped to fit the {budget} character budget; matches are "
        "kept, shown without surrounding context.]"
    )
    # Room reserved for the budget marker so appending it never busts the cap.
    CHAR_BUDGET_RESERVE: ClassVar[int] = 400

    pattern: str = Field(description="Search pattern. If use_regex=True, this is a regex pattern; otherwise plain text.")
    file_pattern: Optional[str] = Field(None, description="Glob pattern to limit which files are searched (e.g., '**/*.py'). If None, use filenames or directory.")
    filenames: Optional[List[str]] = Field(default=None, description="List of file paths to search in. If not provided, use directory or file_pattern.")
    directory: Optional[str] = Field(default=None, description="Directory to search recursively (if filenames not provided).")
    context_lines: int = Field(default=5, description="Number of lines of context to show before and after each match.")
    show_line_numbers: bool = Field(default=True, description="Include line numbers in the output.")
    use_regex: bool = Field(default=True, description="If True, treat pattern as a regular expression.")
    case_sensitive: bool = Field(default=False, description="If True, perform case-sensitive search (default False).")
    max_results: int = Field(default=50, description="Maximum number of matches to return.")
    exclude_dirs: List[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDE_DIRS),
        description="Directory names to exclude from search (exact match or glob patterns)"
    )
    
    def _should_exclude_dir(self, dirname: str) -> bool:
        """Check if a directory should be excluded based on exclude_dirs patterns."""
        for pattern in self.exclude_dirs:
            if fnmatch.fnmatch(dirname, pattern):
                return True
        return False

    def _is_path_excluded(self, file_path: str) -> bool:
        """Check if a file path should be excluded based on exclude_dirs patterns."""
        # Check each directory component
        path = Path(file_path)
        for parent in path.parents:
            if self._should_exclude_dir(parent.name):
                return True
        return False

    @staticmethod
    def _highlight_span(match: dict, line_idx: int, line_offsets: List[int]):
        """Column span (start, end) of ``match`` on 0-based line ``line_idx``."""
        if line_idx < 0 or line_idx + 1 >= len(line_offsets):
            return None
        line_start_pos = line_offsets[line_idx]
        line_end_pos = line_offsets[line_idx + 1]
        seg_start = max(match['start'], line_start_pos)
        seg_end = min(match['end'], line_end_pos)
        if seg_start >= seg_end:
            return None
        return (seg_start - line_start_pos, seg_end - line_start_pos)

    def _format_source_line(self, file_lines: List[str], line_num: int,
                            spans, is_match: bool) -> str:
        """Render one source line, highlighting every span on it exactly once."""
        highlighted = file_lines[line_num - 1]
        if spans:
            # Right-to-left so earlier column offsets stay valid.
            for col_start, col_end in sorted(set(spans), reverse=True):
                highlighted = (highlighted[:col_start] + '**'
                               + highlighted[col_start:col_end] + '**'
                               + highlighted[col_end:])
        line_content = highlighted.rstrip('\n')
        prefix = "> " if is_match else "  "
        if self.show_line_numbers:
            return f"{prefix}{line_num:4d}: {line_content}"
        return f"{prefix}{line_content}"

    @staticmethod
    def _rendered_chars(rendered) -> int:
        return sum(len(text) + 1 for text, _ in rendered)

    def _render_matches_grouped(self, matches_by_file, file_lines_cache,
                                file_line_offsets_cache, context_lines):
        """Render matches grouped per file.

        One ``rel_path:`` header per file; context windows of nearby matches are
        merged so no source line is printed twice; at most
        ``MAX_MATCHES_PER_FILE`` blocks are printed per file and the rest are
        announced explicitly (count + line numbers).

        Returns ``[(text, matches_represented), ...]`` where the second element
        is how many matches that line stands for (0 for context, headers and
        notices), so the caller can account for what a budget drop loses.
        """
        rendered = []
        for file_path, file_matches in matches_by_file.items():
            file_lines = file_lines_cache.get(file_path)
            line_offsets = file_line_offsets_cache.get(file_path)
            if file_lines is None or line_offsets is None:
                continue
            try:
                rel_path = os.path.relpath(file_path)
            except ValueError:
                rel_path = file_path

            windows = []
            for match in file_matches:
                ctx_start = max(1, match['line_start'] - context_lines)
                ctx_end = min(len(file_lines), match['line_end'] + context_lines)
                windows.append((ctx_start, ctx_end, match))
            windows.sort(key=lambda w: (w[0], w[1]))

            merged = []  # [ctx_start, ctx_end, [matches]]
            for ctx_start, ctx_end, match in windows:
                if merged and ctx_start <= merged[-1][1] + 1:
                    merged[-1][1] = max(merged[-1][1], ctx_end)
                    merged[-1][2].append(match)
                else:
                    merged.append([ctx_start, ctx_end, [match]])

            printed = merged[:self.MAX_MATCHES_PER_FILE]
            omitted = merged[self.MAX_MATCHES_PER_FILE:]

            rendered.append((f"{rel_path}:", 0))
            for ctx_start, ctx_end, group in printed:
                spans_by_line = {}
                matched_lines = set()
                for match in group:
                    for line_num in range(match['line_start'], match['line_end'] + 1):
                        matched_lines.add(line_num)
                        span = self._highlight_span(match, line_num - 1, line_offsets)
                        if span is not None:
                            spans_by_line.setdefault(line_num, []).append(span)
                # Charge this block's match count to one line only.
                charge_line = min(m['line_start'] for m in group)
                for line_num in range(ctx_start, ctx_end + 1):
                    rendered.append((
                        self._format_source_line(
                            file_lines, line_num,
                            spans_by_line.get(line_num),
                            line_num in matched_lines,
                        ),
                        len(group) if line_num == charge_line else 0,
                    ))
                rendered.append(("", 0))

            if omitted:
                omitted_lines = sorted(
                    m['line_start'] for group in omitted for m in group[2]
                )
                shown = omitted_lines[:self.MAX_OMITTED_LINES_SHOWN]
                listed = ", ".join(str(n) for n in shown)
                if len(omitted_lines) > len(shown):
                    listed += ", ..."
                rendered.append((
                    self.MORE_MATCHES_NOTICE.format(
                        count=len(omitted_lines), lines=listed
                    ), 0,
                ))
        return rendered

    def execute(self) -> str:
        try:
            # Determine which files to search
            files_to_search = []
            if self.filenames:
                for f in self.filenames:
                    try:
                        validated_f = self._validate_path(f)
                    except ValueError as e:
                        return f"Error: {e}"
                    if os.path.isdir(validated_f):
                        # treat as directory, expand recursively with exclusion
                        for root, dirs, files in os.walk(validated_f):
                            # Modify dirs in-place to exclude unwanted directories
                            dirs[:] = [d for d in dirs if not self._should_exclude_dir(d)]
                            for file in files:
                                full_path = os.path.join(root, file)
                                # Validate each subfile (should be within workspace since root is)
                                try:
                                    validated_sub = self._validate_path(full_path)
                                    files_to_search.append(validated_sub)
                                except ValueError:
                                    # Skip files outside workspace
                                    continue
                    else:
                        files_to_search.append(validated_f)
            elif self.directory:
                # Validate directory path is within workspace
                try:
                    validated_dir = self._validate_path(self.directory)
                except ValueError as e:
                    return f"Error: {e}"
                if not os.path.isdir(validated_dir):
                    return f"Error: '{self.directory}' is not a valid directory."
                for root, dirs, files in os.walk(validated_dir):
                    # Modify dirs in-place to exclude unwanted directories
                    dirs[:] = [d for d in dirs if not self._should_exclude_dir(d)]
                    for file in files:
                        full_path = os.path.join(root, file)
                        # Apply file_pattern filter if set (same logic as file_pattern-only branch)
                        if self.file_pattern:
                            pattern = self.file_pattern
                            rel_path = os.path.relpath(full_path, str(validated_dir))
                            if not fnmatch.fnmatch(file, pattern) and not Path(rel_path).match(pattern):
                                continue
                        # Validate each subfile (should be within workspace since root is)
                        try:
                            validated_sub = self._validate_path(full_path)
                            files_to_search.append(validated_sub)
                        except ValueError:
                            # Skip files outside workspace
                            continue
            elif self.file_pattern:
                # Walk directory tree with exclusion pruning (unlike Path.glob which traverses everything)
                files_to_search = []
                ws_path = self._resolve_workspace_root()
                base_path = Path(ws_path or '.')
                pattern = self.file_pattern
                for root, dirs, files in os.walk(str(base_path)):
                    # Prune excluded directories BEFORE traversing into them
                    dirs[:] = [d for d in dirs if not self._should_exclude_dir(d)]
                    for file in files:
                        full_path = os.path.join(root, file)
                        rel_path = os.path.relpath(full_path, str(base_path))
                        # Match file against glob pattern:
                        # fnmatch handles simple patterns like '*.py', 'test_*.py' (filename only)
                        # Path.match handles complex patterns like '**/*.py', 'tools/*.py' (relative path)
                        if not fnmatch.fnmatch(file, pattern) and not Path(rel_path).match(pattern):
                            continue
                        try:
                            validated_path = self._validate_path(full_path)
                            files_to_search.append(validated_path)
                        except ValueError:
                            continue
            else:
                return "Error: Provide one of 'filenames', 'directory', or 'file_pattern'."
            
            # Limit number of files to prevent excessive scanning (optional)
            if len(files_to_search) > self.MAX_FILES_TO_SEARCH:
                return f"Error: Too many files to search ({len(files_to_search)}). Please narrow your search."
            
            # Prepare pattern
            if self.use_regex:
                flags = 0 if self.case_sensitive else re.IGNORECASE
                regex_pattern = re.compile(self.pattern, flags)
            else:
                # escape special regex characters, treat as literal
                flags = 0 if self.case_sensitive else re.IGNORECASE
                regex_pattern = re.compile(re.escape(self.pattern), flags)
            
            # Initialize truncation tracking (MUST be before any truncation checks)
            truncation_warnings = []
            line_truncated_files = set()
            results_truncated = False

            # Apply safety limits to parameters
            context_lines = min(self.context_lines, self.MAX_CONTEXT_LINES)
            if self.context_lines > self.MAX_CONTEXT_LINES:
                truncation_warnings.append(
                    "[Output truncated at {} context lines. Use more specific parameters or pagination to read further.]".format(self.MAX_CONTEXT_LINES)
                )
            max_results = min(self.max_results, self.MAX_RESULTS)
            
            matches = []
            file_lines_cache = {}
            file_line_offsets_cache = {}
            nonexistent_files = []
            skipped_large_files = 0

            for file_path in files_to_search:
                if not os.path.isfile(file_path):
                    nonexistent_files.append(file_path)
                    continue
                try:
                    file_size = os.path.getsize(file_path)
                    if file_size > self.MAX_FILE_SIZE:
                        skipped_large_files += 1
                        continue
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                    lines = content.splitlines(keepends=True)
                    if len(lines) > self.MAX_LINES_PER_FILE:
                        lines = lines[:self.MAX_LINES_PER_FILE]
                        content = ''.join(lines)
                        line_truncated_files.add(file_path)
                    file_lines_cache[file_path] = lines
                    line_offsets = [0]
                    for line in lines:
                        line_offsets.append(line_offsets[-1] + len(line))
                    file_line_offsets_cache[file_path] = line_offsets
                except (IOError, PermissionError, UnicodeDecodeError):
                    continue

                # Find all matches
                for match in regex_pattern.finditer(content):
                    start = match.start()
                    end = match.end()
                    # Compute line numbers efficiently using binary search on offsets
                    line_start = bisect.bisect_right(line_offsets, start)
                    # For line_end, use end-1 to get line containing last char
                    line_end = bisect.bisect_right(line_offsets, end - 1) if end > start else line_start
                    matches.append({
                        'file': file_path,
                        'start': start,
                        'end': end,
                        'line_start': line_start,
                        'line_end': line_end,
                        'match_text': match.group()
                    })
                    if len(matches) >= max_results:
                        results_truncated = True
                        break
                if len(matches) >= max_results:
                    results_truncated = True
                    break            
            if not matches:
                mode = "regex" if self.use_regex else "plain text"
                if not self.use_regex:
                    # Check if pattern looks like it might be intended as regex
                    import re as _re
                    # Strong regex indicators: escaped chars (\., \), \|, etc.) or standalone | outside character classes
                    # Avoid flagging plain '.' or '*' alone which are common in non-regex patterns
                    has_regex_indicators = bool(_re.search(r'\\.', self.pattern)) or bool(_re.search(r'(?<!\\)\|', self.pattern))
                else:
                    has_regex_indicators = False

                msg = f"No matches found for pattern '{self.pattern}' ({mode}, case_sensitive={self.case_sensitive})."

                # Add count of files searched
                if files_to_search:
                    total_searched = len(files_to_search) - len(nonexistent_files) - skipped_large_files
                    msg += f"\n  Files searched: {total_searched}"
                    if nonexistent_files:
                        msg += f" ({len(nonexistent_files)} non-existent)"
                    if skipped_large_files:
                        msg += f" ({skipped_large_files} skipped due to size >{self.MAX_FILE_SIZE/1_000_000:.0f}MB)"

                # Add hint for possible regex patterns
                if has_regex_indicators and not self.use_regex:
                    msg += "\n  Hint: The pattern contains special characters (\\, ., (), |, etc). Did you mean to set use_regex=True?"

                # Add nonexistent files detail
                if nonexistent_files:
                    if self.filenames and len(nonexistent_files) == len(files_to_search):
                        msg = f"None of the specified files exist: {', '.join(self.filenames)}"
                    elif nonexistent_files and len(nonexistent_files) <= 5:
                        rel_nonexistent = []
                        for f in nonexistent_files:
                            try:
                                rel_nonexistent.append(os.path.relpath(f))
                            except ValueError:
                                rel_nonexistent.append(f)
                        msg += f"\n  Non-existent files: {', '.join(rel_nonexistent)}"

                return msg
            
            # Group matches by file, preserving first-seen file order, so each
            # file's path is printed exactly once.
            matches_by_file = {}
            for match in matches:
                matches_by_file.setdefault(match['file'], []).append(match)

            rendered = self._render_matches_grouped(
                matches_by_file, file_lines_cache, file_line_offsets_cache,
                context_lines,
            )

            header = f"Found {len(matches)} matches for pattern '{self.pattern}'"
            if self.max_results != max_results:
                header += f" (clamped from {self.max_results} to {max_results})"
            if self.use_regex:
                header += " (regex)"
            else:
                header += " (plain text)"
            if self.directory:
                header += f" in directory '{self.directory}'"
            header += f" (case_sensitive={self.case_sensitive}, max_results={self.max_results}):"

            # Prefer many DISTINCT matches over one match's context: if the
            # full-context rendering blows the character budget, re-render with
            # no context lines before any match is dropped.
            if (context_lines > 0
                    and self._rendered_chars(rendered) + len(header) + 1
                    > self.MAX_OUTPUT_CHARS):
                rendered = self._render_matches_grouped(
                    matches_by_file, file_lines_cache, file_line_offsets_cache, 0
                )
                truncation_warnings.append(
                    self.CONTEXT_DROPPED_WARNING.format(budget=self.MAX_OUTPUT_CHARS)
                )

            if line_truncated_files:
                truncation_warnings.append(
                    "[Output truncated at {} lines per file. Use more specific parameters or pagination to read further.]".format(self.MAX_LINES_PER_FILE)
                )
            if results_truncated:
                truncation_warnings.append(
                    "[Output truncated at {} results. Use more specific parameters or pagination to read further.]".format(max_results)
                )

            trailer = ("\n" + "\n".join(truncation_warnings)) if truncation_warnings else ""
            fixed_chars = len(header) + 1 + len(trailer)

            # Enforce the character budget: stop adding blocks that no longer
            # fit and count the matches they stood for, so the marker can say
            # exactly how much was dropped.
            kept = []
            used = fixed_chars
            dropped_matches = 0
            budget_hit = False
            for text, n_matches in rendered:
                if budget_hit or (used + len(text) + 1 + self.CHAR_BUDGET_RESERVE
                                  > self.MAX_OUTPUT_CHARS):
                    budget_hit = True
                    dropped_matches += n_matches
                    continue
                kept.append((text, n_matches))
                used += len(text) + 1

            def _compose():
                body = "\n".join(text for text, _ in kept)
                out = (header + "\n" + body) if body else header
                if budget_hit:
                    out += "\n" + self.CHAR_BUDGET_WARNING.format(
                        budget=self.MAX_OUTPUT_CHARS, dropped=dropped_matches
                    )
                if trailer:
                    out += trailer
                return out

            output = _compose()
            # Hard guarantee: never exceed MAX_OUTPUT_CHARS, even with the
            # warnings/marker appended.
            while len(output) > self.MAX_OUTPUT_CHARS and kept:
                _, n_matches = kept.pop()
                dropped_matches += n_matches
                budget_hit = True
                output = _compose()

            return self._truncate_output(output)
            
        except Exception as e:
            return self._truncate_output(f"Error searching files: {e}")
