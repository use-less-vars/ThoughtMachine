#!/usr/bin/env python3
"""
Copy all updated files from construction workspace to stable workspace.
Excludes temporary files, cache directories, and test files.
"""

import os
import shutil
from pathlib import Path

def copy_construction_to_stable():
    # Determine stable root directory (where this script is located)
    stable_root = None
    
    # Try multiple methods to get the stable root
    methods = [
        # Method 1: Use script's directory (most reliable)
        lambda: Path(__file__).parent.resolve(),
        # Method 2: Resolve current directory
        lambda: Path(".").resolve(),
        # Method 3: Use current working directory
        lambda: Path(os.getcwd()).resolve(),
        # Method 4: Use parent of construction directory if we can find it
        lambda: Path("./construction").parent.resolve(),
    ]
    
    for method in methods:
        try:
            stable_root = method()
            # Verify it's a directory
            if stable_root.is_dir():
                break
        except (FileNotFoundError, OSError) as e:
            continue
    
    if stable_root is None:
        print("Error: Could not determine stable root directory.")
        print("Please run this script from within the stable workspace.")
        return
    
    print(f"Stable root directory: {stable_root}")
    construction_root = stable_root / "construction"


    
    if not construction_root.exists():
        print("Construction directory not found.")
        return
    
    # List of files/directories to exclude (relative to construction root)
    exclude_dirs = {
        "__pycache__",
        "temp",
        ".git",
        ".vscode",
        ".idea",
        "__pycache__",
    }
    
    exclude_files = {
        "test2.txt",
        "test_construction2.txt",
        "copy_construction_to_stable.py",  # don't copy this script
        "improvement_report.txt",          # don't copy the report
    }
    
    files_copied = 0
    errors = []
    
    # Walk through construction directory
    for src_path in construction_root.rglob("*"):
        # Skip directories
        if src_path.is_dir():
            continue
        
        # Skip hidden files (starting with .)
        if src_path.name.startswith('.'):
            continue
        
        # Skip Python cache files
        if src_path.suffix in ('.pyc', '.pyo', '.pyd'):
            continue
        
        # Skip excluded directories in path
        skip = False
        for part in src_path.parts:
            if part in exclude_dirs:
                skip = True
                break
        if skip:
            continue
        
        # Skip specific excluded files
        if src_path.name in exclude_files:
            continue
        
        # Compute relative path to construction root
        try:
            rel_path = src_path.relative_to(construction_root)
        except ValueError:
            continue
        
        dest_path = stable_root / rel_path
        
        # Ensure destination directory exists
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Check if file needs updating (compare modification times)
        copy_needed = True
        if dest_path.exists():
            src_mtime = src_path.stat().st_mtime
            dest_mtime = dest_path.stat().st_mtime
            if src_mtime <= dest_mtime:
                copy_needed = False
        
        if copy_needed:
            try:
                shutil.copy2(src_path, dest_path)
                print(f"✓ Copied: {rel_path}")
                files_copied += 1
            except Exception as e:
                errors.append(f"Error copying {rel_path}: {e}")
                print(f"✗ Error: {rel_path} - {e}")
        else:
            print(f"  Skipped (up to date): {rel_path}")
    
    print(f"\n{'='*60}")
    print(f"Migration Summary")
    print(f"{'='*60}")
    print(f"Files copied: {files_copied}")
    if errors:
        print(f"Errors: {len(errors)}")
        for error in errors:
            print(f"  {error}")
    
    print(f"\nImportant files updated:")
    print(f"- agent_core.py (improved system prompt loading)")
    print(f"- system_prompt.txt (added directory structure diagnosis)")
    print(f"- qt_gui_updated.py (bug fixes and robustness improvements)")
    print(f"- tools/ (if any updates)")
    
    print(f"\nNext steps:")
    print(f"1. Review the improvement_report.txt for detailed analysis")
    print(f"2. Test the updated system: python qt_gui_updated.py")
    print(f"3. Run agent tests to verify functionality")
    print(f"4. Commit changes: git add . && git commit -m 'Migrate improvements from construction workspace'")
    
    if errors:
        print(f"\n⚠️  WARNING: Some files failed to copy. Please check errors above.")

if __name__ == "__main__":
    copy_construction_to_stable()