"""
RefactorTool - Apply structural code modifications across multiple files.
Combines the structural understanding of CodeModifier with the multi‑file capabilities of ApplyEdits.
"""
from pathlib import Path
from pydantic import Field
from typing import List, Dict, Any, Optional
import difflib
import tempfile
import shutil
import glob

from .base import ToolBase
from .code_modifier_utils import apply_code_modifier, compute_diff
def _commit_changes_atomically(modified_contents):
    """
    Atomically write modified contents to files.
    modified_contents: dict Path -> (original, new)
    Raises exception on failure, with rollback performed.
    """
    import shutil
    import tempfile
    from pathlib import Path
    
    backups = []
    temps = []
    try:
        for file_path, (original, new) in modified_contents.items():
            # create backup
            backup_path = file_path.with_suffix(file_path.suffix + '.refactor_backup')
            shutil.copy2(file_path, backup_path)
            backups.append((file_path, backup_path))
            # create temp file
            with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8',
                dir=file_path.parent,
                delete=False,
                suffix='.refactor_tmp'
            ) as tf:
                tf.write(new)
                temp_path = Path(tf.name)
            temps.append((file_path, temp_path))
        # all backups and temps created, now commit
        for file_path, temp_path in temps:
            # rename temp to original (atomic on POSIX, Windows may need special handling)
            temp_path.replace(file_path)
        # success, remove backups
        for _, backup_path in backups:
            backup_path.unlink()
    except Exception as e:
        # rollback: restore from backups
        for file_path, backup_path in backups:
            if backup_path.exists():
                shutil.copy2(backup_path, file_path)
                backup_path.unlink()
        # delete any temp files
        for _, temp_path in temps:
            if temp_path.exists():
                temp_path.unlink()
        raise




class RefactorTool(ToolBase):
    """
    Apply CodeModifier operations to all files matching a glob pattern.
    Supports preview mode and atomic application (all files succeed or none are written).
    """
    operation: str = Field(description="Type of refactoring operation. One of: 'add_method', 'rename_function', 'replace_function_body', 'add_import', 'add_class', 'modify_function'.")
    target: str = Field(description="Name of the target code element (e.g., class name, function name).")
    file_pattern: str = Field(description="Glob pattern to select files (e.g., '**/*.py').")
    params: dict = Field(description="Additional parameters required for the operation. See CodeModifier for details.")
    preview: bool = Field(False, description="If True, only show diff(s) without modifying any files.")
    
    def _get_file_paths(self) -> List[Path]:
        """Return list of Path objects for files to edit based on file_pattern."""
        pattern = self.file_pattern
        # Determine base directory for globbing
        if self.workspace_path:
            base_dir = Path(self.workspace_path)
        else:
            base_dir = Path('.')
        # Use glob recursively if pattern contains **
        if '**' in pattern:
            raw_paths = list(base_dir.glob(pattern))
        else:
            raw_paths = list(base_dir.glob(pattern))
        validated_paths = []
        for p in raw_paths:
            try:
                validated = self._validate_path(str(p))
                validated_paths.append(Path(validated))
            except ValueError:
                # Skip files outside workspace
                continue
        return validated_paths
    
    def _map_operation_and_params(self) -> tuple[str, dict]:
        """
        Map RefactorTool operation and target to CodeModifier operation and parameters.
        Returns (operation, params) where operation is one of CodeModifier's operations.
        """
        op = self.operation
        target = self.target
        params = self.params.copy()
        
        # Mapping from RefactorTool operation to CodeModifier operation
        op_map = {
            'add_method': 'add_method',
            'rename_function': 'modify_function',
            'replace_function_body': 'replace_function_body',
            'add_import': 'add_import',
            'add_class': 'add_class',
            'modify_function': 'modify_function',
        }
        if op not in op_map:
            raise ValueError(f"Unsupported operation: {op}")
        code_op = op_map[op]
        
        # Map target to appropriate parameter based on operation
        if op == 'add_method':
            # target is the class name
            if 'class_name' not in params:
                params['class_name'] = target
        elif op == 'rename_function':
            # target is the function name to rename
            if 'name' not in params:
                params['name'] = target
            # ensure new_name is provided in params (required)
        elif op == 'replace_function_body':
            # target is the function name
            if 'target' not in params:
                params['target'] = target
        elif op == 'add_import':
            # target is the module to import
            if 'import_module' not in params:
                params['import_module'] = target
        elif op == 'add_class':
            # target is the new class name
            if 'name' not in params:
                params['name'] = target
        elif op == 'modify_function':
            # target is the function name
            if 'name' not in params:
                params['name'] = target
        
        # Ensure required fields for each operation are present (validation will happen later)
        return code_op, params
    
    def execute(self) -> str:
        """Execute the refactoring across all matching files."""
        from pathlib import Path
        
        try:
            file_paths = self._get_file_paths()
        except Exception as e:
            return f"Error expanding file pattern: {e}"
        
        if not file_paths:
            return "No files matched the pattern."
        
        # Map operation and params
        try:
            code_op, params = self._map_operation_and_params()
        except ValueError as e:
            return f"Error: {e}"
        
        # Process each file, collect results
        results = []
        diffs = []
        errors = []
        modified_contents = {}
        
        for file_path in file_paths:
            # Read original content (we'll need it for diff and fallback)
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    original = f.read()
            except Exception as e:
                errors.append(f"{file_path}: read error - {e}")
                continue
            
            # Apply the modification in memory
            success, new_content, error_msg = apply_code_modifier(
                file_path, code_op, **params
            )
            
            if not success:
                errors.append(f"{file_path}: {error_msg}")
                continue
            
            # Store modified content for later write (if preview=False)
            modified_contents[file_path] = (original, new_content)
            
            # Compute diff
            diff = compute_diff(original, new_content, file_path)
            diffs.append(diff)
            results.append(f"{file_path}: OK")
        
        # If preview mode, just return diffs and summary
        if self.preview:
            summary = f"Preview mode: {len(results)} files would be modified, {len(errors)} errors."
            if results:
                summary += "\n\nSuccessful files:\n" + "\n".join(results)
            if diffs:
                summary += "\n\nDiffs:\n" + "\n".join(diffs)
            if errors:
                summary += "\n\nErrors:\n" + "\n".join(errors)
            return summary
        
        # Otherwise, apply changes atomically: only if all files succeeded
        if errors:
            return f"Cannot apply changes because {len(errors)} file(s) failed:\n" + "\n".join(errors)
        
        # Apply changes atomically
        try:
            _commit_changes_atomically(modified_contents)
        except Exception as e:
            return f"Error applying changes atomically: {e}. No files were modified."
        summary = f"Successfully modified {len(results)} file(s)."
        if diffs:
            summary += "\n\nDiffs:\n" + "\n".join(diffs)
        return summary