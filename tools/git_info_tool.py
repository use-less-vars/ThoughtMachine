# tools/git_info_tool.py
from typing import Literal, Optional, List
from pydantic import Field
import subprocess
import os
from pathlib import Path
from .base import ToolBase


class GitInfoTool(ToolBase):
    """
    Read-only Git repository information tool.
    Provides access to git status, diff, log, branch, show, remote, blame, and config.
    All operations are read-only and cannot modify the repository.
    """
    tool: Literal["GitInfoTool"] = "GitInfoTool"

    
    operation: Literal["status", "diff", "log", "branch", "show", "remote", "blame", "config"] = Field(
        description="Git operation to perform: status, diff, log, branch, show, remote, blame, config"
    )
    
    # Common parameters
    working_dir: Optional[str] = Field(
        default=None,
        description="Path to git repository root (defaults to workspace root)"
    )
    
    # Operation-specific parameters
    commit1: Optional[str] = Field(
        default=None,
        description="First commit reference for diff operation (default: HEAD)"
    )
    commit2: Optional[str] = Field(
        default=None,
        description="Second commit reference for diff operation (default: working tree)"
    )
    file_path: Optional[str] = Field(
        default=None,
        description="File path for diff, log, or blame operations"
    )
    
    # Log parameters
    max_count: Optional[int] = Field(
        default=50,
        description="Maximum number of commits to show for log operation"
    )
    since: Optional[str] = Field(
        default=None,
        description="Show commits more recent than specified date for log operation"
    )
    until: Optional[str] = Field(
        default=None,
        description="Show commits older than specified date for log operation"
    )
    author: Optional[str] = Field(
        default=None,
        description="Filter commits by author for log operation"
    )
    grep: Optional[str] = Field(
        default=None,
        description="Filter commits by commit message pattern for log operation"
    )
    
    # Branch parameters
    all_branches: bool = Field(
        default=False,
        description="Include remote branches for branch operation"
    )
    
    # Show parameters
    commit: Optional[str] = Field(
        default="HEAD",
        description="Commit reference for show operation"
    )
    format: Optional[str] = Field(
        default=None,
        description="Format string for show operation (e.g., '%H %s')"
    )
    
    # Blame parameters
    line_start: Optional[int] = Field(
        default=None,
        description="Start line number for blame operation"
    )
    line_end: Optional[int] = Field(
        default=None,
        description="End line number for blame operation"
    )
    
    # Config parameters
    config_name: Optional[str] = Field(
        default=None,
        description="Config name to retrieve (if not specified, list all configs)"
    )
    
    def execute(self) -> str:
        try:
            # Determine working directory
            if self.working_dir:
                # Validate working_dir is within workspace
                try:
                    validated_working_dir = self._validate_path(self.working_dir)
                except ValueError as e:
                    return self._truncate_output(f"Error: {e}")
                repo_root = Path(validated_working_dir).expanduser().resolve()
            elif self.workspace_path:
                # workspace_path is already validated by agent
                repo_root = Path(self.workspace_path).expanduser().resolve()
            else:
                repo_root = Path.cwd()
            
            # Validate git repository
            git_dir = repo_root / ".git"
            if not git_dir.exists() and not git_dir.is_dir():
                # Try to find git root
                try:
                    result = subprocess.run(
                        ["git", "rev-parse", "--show-toplevel"],
                        cwd=repo_root,
                        capture_output=True,
                        text=True,
                        timeout=10
                    )
                    if result.returncode != 0:
                        return self._truncate_output(f"Not a git repository: {repo_root}")
                    repo_root = Path(result.stdout.strip())
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    return self._truncate_output(f"Git not available or not a git repository: {repo_root}")
            
            # Execute operation
            if self.operation == "status":
                return self._git_status(repo_root)
            elif self.operation == "diff":
                return self._git_diff(repo_root)
            elif self.operation == "log":
                return self._git_log(repo_root)
            elif self.operation == "branch":
                return self._git_branch(repo_root)
            elif self.operation == "show":
                return self._git_show(repo_root)
            elif self.operation == "remote":
                return self._git_remote(repo_root)
            elif self.operation == "blame":
                return self._git_blame(repo_root)
            elif self.operation == "config":
                return self._git_config(repo_root)
            else:
                return self._truncate_output(f"Unknown operation: {self.operation}")
        
        except Exception as e:
            return self._truncate_output(f"Error executing git operation: {e}")
    
    def _run_git(self, repo_root: Path, args: List[str], timeout: int = 30) -> str:
        """Run git command and return output."""
        try:
            result = subprocess.run(
                ["git"] + args,
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False  # We'll handle errors manually
            )
            if result.returncode != 0:
                return f"Git command failed (exit code {result.returncode}):\n{result.stderr}"
            return result.stdout
        except subprocess.TimeoutExpired:
            return "Git command timed out"
        except FileNotFoundError:
            return "Git command not found (git may not be installed)"
        except Exception as e:
            return f"Error running git command: {e}"
    
    def _git_status(self, repo_root: Path) -> str:
        """Run git status."""
        output = self._run_git(repo_root, ["status", "--porcelain=v1"])
        if output.startswith("Git command failed"):
            # Try human-readable status
            output = self._run_git(repo_root, ["status"])
        return self._truncate_output(output)
    
    def _git_diff(self, repo_root: Path) -> str:
        """Run git diff."""
        args = ["diff"]
        if self.commit1:
            args.append(self.commit1)
        if self.commit2:
            args.append(self.commit2)
        else:
            # If only commit1 is specified, compare commit1 to working tree
            pass
        if self.file_path:
            # Validate file path is within workspace
            try:
                # Compute absolute path relative to repo_root
                file_abs = (repo_root / self.file_path).resolve()
                validated_abs = self._validate_path(str(file_abs))
                # Convert to path relative to repo_root for git
                file_rel = Path(validated_abs).relative_to(repo_root)
                args.append("--")
                args.append(str(file_rel))
            except ValueError as e:
                return self._truncate_output(f"Error: {e}")
        output = self._run_git(repo_root, args)
        return self._truncate_output(output)
    
    def _git_log(self, repo_root: Path) -> str:
        """Run git log."""
        args = ["log", f"--max-count={self.max_count}", "--oneline"]
        if self.since:
            args.append(f"--since={self.since}")
        if self.until:
            args.append(f"--until={self.until}")
        if self.author:
            args.append(f"--author={self.author}")
        if self.grep:
            args.append(f"--grep={self.grep}")
        if self.file_path:
            # Validate file path is within workspace
            try:
                # Compute absolute path relative to repo_root
                file_abs = (repo_root / self.file_path).resolve()
                validated_abs = self._validate_path(str(file_abs))
                # Convert to path relative to repo_root for git
                file_rel = Path(validated_abs).relative_to(repo_root)
                args.append("--")
                args.append(str(file_rel))
            except ValueError as e:
                return self._truncate_output(f"Error: {e}")
        output = self._run_git(repo_root, args)
        return self._truncate_output(output)
    
    def _git_branch(self, repo_root: Path) -> str:
        """Run git branch."""
        args = ["branch"]
        if self.all_branches:
            args.append("-a")
        output = self._run_git(repo_root, args)
        return self._truncate_output(output)
    
    def _git_show(self, repo_root: Path) -> str:
        """Run git show."""
        args = ["show"]
        if self.format:
            args.append(f"--format={self.format}")
        args.append(self.commit)
        output = self._run_git(repo_root, args)
        return self._truncate_output(output)
    
    def _git_remote(self, repo_root: Path) -> str:
        """Run git remote."""
        output = self._run_git(repo_root, ["remote", "-v"])
        return self._truncate_output(output)
    
    def _git_blame(self, repo_root: Path) -> str:
        """Run git blame."""
        if not self.file_path:
            return "Error: file_path is required for blame operation"
        # Validate file path is within workspace
        try:
            # Compute absolute path relative to repo_root
            file_abs = (repo_root / self.file_path).resolve()
            validated_abs = self._validate_path(str(file_abs))
            # Convert to path relative to repo_root for git
            file_rel = Path(validated_abs).relative_to(repo_root)
        except ValueError as e:
            return self._truncate_output(f"Error: {e}")
        
        args = ["blame"]
        if self.line_start and self.line_end:
            args.append(f"-L{self.line_start},{self.line_end}")
        elif self.line_start:
            args.append(f"-L{self.line_start},+1")
        args.append("--")
        args.append(str(file_rel))
        output = self._run_git(repo_root, args)
        return self._truncate_output(output)
    
    def _git_config(self, repo_root: Path) -> str:
        """Run git config."""
        args = ["config", "--list"]
        if self.config_name:
            args = ["config", "--get", self.config_name]
        output = self._run_git(repo_root, args)
        return self._truncate_output(output)
