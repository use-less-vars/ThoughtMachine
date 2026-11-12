"""Tests for the read-only, path-confined ``ReadFileTool`` (main agent).

Every rejection is asserted on its exact machine-checkable ``reason`` code so
the escape vectors (absolute path / lexical ``..`` / realpath-via-symlink /
missing-or-directory) can never be conflated with one another.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tools.read_file_confined_tool import ReadFileTool


MODULE_PATH = Path(ReadFileTool.__module__.replace(".", "/") + ".py")


def _reason(payload: str) -> str:
    return json.loads(payload).get("reason", "")


class _WorkspaceCase(unittest.TestCase):
    """Shared temp workspace + an 'outside' tree used for escape attempts."""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="rft-ws-")
        self.outside = tempfile.mkdtemp(prefix="rft-out-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(shutil.rmtree, self.outside, True)
        (Path(self.root) / "hello.txt").write_text("hello\nworld\n", encoding="utf-8")
        (Path(self.root) / "sub").mkdir()
        (Path(self.root) / "sub" / "nested.txt").write_text("nested\n", encoding="utf-8")
        (Path(self.outside) / "secret.txt").write_text("TOP SECRET\n", encoding="utf-8")

    def run_tool(self, **kwargs) -> str:
        kwargs.setdefault("workspace_path", self.root)
        return ReadFileTool(**kwargs).execute()


class TestHappyPath(_WorkspaceCase):
    def test_reads_whole_file(self) -> None:
        out = json.loads(self.run_tool(file_path="hello.txt"))
        self.assertEqual(out["content"], "hello\nworld")
        self.assertEqual(out["total_lines"], 2)
        self.assertEqual((out["start_line"], out["end_line"]), (1, 2))

    def test_reads_nested_relative_path(self) -> None:
        out = json.loads(self.run_tool(file_path="sub/nested.txt"))
        self.assertEqual(out["content"], "nested")

    def test_empty_file(self) -> None:
        (Path(self.root) / "empty.txt").write_text("", encoding="utf-8")
        out = json.loads(self.run_tool(file_path="empty.txt"))
        self.assertEqual(out["content"], "")
        self.assertEqual(out["total_lines"], 0)


class TestAbsolutePath(_WorkspaceCase):
    def test_absolute_outside_root(self) -> None:
        out = self.run_tool(file_path=str(Path(self.outside) / "secret.txt"))
        self.assertEqual(_reason(out), "absolute_path")

    def test_absolute_inside_root_still_rejected(self) -> None:
        # Even a path that IS inside the root must be expressed relatively:
        # the absolute form is refused before any join happens.
        out = self.run_tool(file_path=str(Path(self.root) / "hello.txt"))
        self.assertEqual(_reason(out), "absolute_path")


class TestParentTraversal(_WorkspaceCase):
    def test_lexical_dotdot_escapes(self) -> None:
        out = self.run_tool(file_path="../" + Path(self.outside).name + "/secret.txt")
        self.assertEqual(_reason(out), "parent_traversal")

    def test_dotdot_that_would_resolve_back_inside(self) -> None:
        out = self.run_tool(file_path="sub/../hello.txt")
        self.assertEqual(_reason(out), "parent_traversal")

    def test_nested_dotdot(self) -> None:
        out = self.run_tool(file_path="sub/../../etc/passwd")
        self.assertEqual(_reason(out), "parent_traversal")


class TestSymlinkEscape(_WorkspaceCase):
    def test_symlink_to_outside_file_rejected(self) -> None:
        link = Path(self.root) / "escape.txt"
        os.symlink(Path(self.outside) / "secret.txt", link)
        out = self.run_tool(file_path="escape.txt")
        self.assertEqual(_reason(out), "symlink_escape")

    def test_symlink_to_outside_directory_rejected(self) -> None:
        os.symlink(self.outside, Path(self.root) / "outdir")
        out = self.run_tool(file_path="outdir/secret.txt")
        self.assertEqual(_reason(out), "symlink_escape")

    def test_symlink_inside_root_to_inside_file_allowed(self) -> None:
        # No over-blocking: an intra-root symlink must keep working.
        os.symlink(Path(self.root) / "hello.txt", Path(self.root) / "alias.txt")
        out = json.loads(self.run_tool(file_path="alias.txt"))
        self.assertEqual(out["content"], "hello\nworld")
        self.assertEqual(out["total_lines"], 2)

    def test_relative_symlink_inside_root_allowed(self) -> None:
        os.symlink("sub/nested.txt", Path(self.root) / "rel_link.txt")
        out = json.loads(self.run_tool(file_path="rel_link.txt"))
        self.assertEqual(out["content"], "nested")


class TestMissingAndNotAFile(_WorkspaceCase):
    def test_missing_file(self) -> None:
        out = self.run_tool(file_path="nope.txt")
        self.assertEqual(_reason(out), "file_not_found")
        self.assertIn("File not found", json.loads(out)["error"])

    def test_directory_instead_of_file(self) -> None:
        out = self.run_tool(file_path="sub")
        self.assertEqual(_reason(out), "not_a_file")
        self.assertIn("directory", json.loads(out)["error"])

    def test_empty_path(self) -> None:
        self.assertEqual(_reason(self.run_tool(file_path="   ")), "invalid_path")


class TestSizeAndBinaryGuards(_WorkspaceCase):
    def test_oversized_file_rejected_with_existing_wording(self) -> None:
        # Reuses FilePreviewTool's guard: same max_file_size field/wording.
        (Path(self.root) / "big.txt").write_text("x" * 5_000, encoding="utf-8")
        out = self.run_tool(file_path="big.txt", max_file_size=1_000)
        self.assertEqual(_reason(out), "file_too_large")
        self.assertIn("File is too large", json.loads(out)["error"])
        self.assertIn("1,000 bytes limit", json.loads(out)["error"])

    def test_default_size_limit_is_file_preview_default(self) -> None:
        self.assertEqual(ReadFileTool.model_fields["max_file_size"].default, 10_000_000)

    def test_binary_file_rejected(self) -> None:
        Path(self.root, "blob.bin").write_bytes(b"\x00\x01\x02\xff\xfe binary")
        self.assertEqual(_reason(self.run_tool(file_path="blob.bin")), "binary_file")


class TestLineRange(_WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        (Path(self.root) / "five.txt").write_text(
            "one\ntwo\nthree\nfour\nfive\n", encoding="utf-8"
        )

    def test_inclusive_range(self) -> None:
        out = json.loads(self.run_tool(file_path="five.txt", start_line=2, end_line=3))
        self.assertEqual(out["content"], "two\nthree")
        self.assertEqual((out["start_line"], out["end_line"]), (2, 3))
        self.assertEqual(out["total_lines"], 5)

    def test_single_line(self) -> None:
        out = json.loads(self.run_tool(file_path="five.txt", start_line=5, end_line=5))
        self.assertEqual(out["content"], "five")

    def test_start_only_runs_to_eof(self) -> None:
        out = json.loads(self.run_tool(file_path="five.txt", start_line=4))
        self.assertEqual(out["content"], "four\nfive")
        self.assertEqual(out["end_line"], 5)

    def test_end_only_starts_at_line_one(self) -> None:
        out = json.loads(self.run_tool(file_path="five.txt", end_line=2))
        self.assertEqual(out["content"], "one\ntwo")
        self.assertEqual(out["start_line"], 1)

    def test_start_below_one_rejected(self) -> None:
        out = self.run_tool(file_path="five.txt", start_line=0)
        self.assertEqual(_reason(out), "invalid_line_range")
        self.assertIn("start_line must be >= 1", json.loads(out)["error"])

    def test_end_before_start_rejected(self) -> None:
        out = self.run_tool(file_path="five.txt", start_line=3, end_line=2)
        self.assertEqual(_reason(out), "invalid_line_range")

    def test_end_past_eof_rejected_not_clamped(self) -> None:
        out = self.run_tool(file_path="five.txt", start_line=4, end_line=99)
        self.assertEqual(_reason(out), "line_range_out_of_bounds")
        self.assertIn("file has 5 lines", json.loads(out)["error"])


class TestRegistration(unittest.TestCase):
    """Wired into the real registry, with a read-only grain."""

    def test_registered_in_tool_registries(self) -> None:
        import tools

        self.assertIn(ReadFileTool, tools.TOOL_CLASSES)
        self.assertIn(ReadFileTool, tools.SIMPLIFIED_TOOL_CLASSES)
        self.assertIs(tools.get_tool_class("ReadFileTool"), ReadFileTool)

    def test_stable_tool_name(self) -> None:
        self.assertEqual(ReadFileTool.tool_name(), "ReadFileTool")

    def test_read_only_grain_only(self) -> None:
        cats = ReadFileTool.get_required_categories({})
        self.assertEqual(cats, ["filesystem:read"])
        self.assertNotIn("filesystem:write", cats)

    def test_present_in_agent_presets(self) -> None:
        from session.tool_presets import AGENT_TOOLS, _ALL_TOOLS

        self.assertIn("ReadFileTool", _ALL_TOOLS)
        self.assertIn("ReadFileTool", AGENT_TOOLS)


class TestReadOnlySurface(unittest.TestCase):
    """Static proof the module cannot mutate anything or span processes."""

    FORBIDDEN_IMPORTS = {
        "subprocess", "shutil", "socket", "http", "urllib", "requests",
        "ftplib", "smtplib", "telnetlib", "asyncio",
    }
    FORBIDDEN_ATTRS = {
        "remove", "unlink", "rename", "replace", "rmdir", "mkdir", "makedirs",
        "chmod", "chown", "truncate", "write_text", "write_bytes", "system",
        "popen", "fork", "execv", "spawn", "symlink", "link",
    }
    FORBIDDEN_NAMES = {"exec", "eval", "compile", "open_connection"}
    MUTATING_MODULES = {"os", "shutil", "pathlib"}

    def _tree(self) -> ast.Module:
        src = Path(MODULE_PATH).read_text(encoding="utf-8")
        return ast.parse(src)

    def test_no_process_or_network_imports(self) -> None:
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], self.FORBIDDEN_IMPORTS)
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or "").split(".")[0], self.FORBIDDEN_IMPORTS)

    @staticmethod
    def _root_name(node: ast.AST):
        """Name the module an attribute chain starts at (``os.path.join`` -> os)."""
        while isinstance(node, ast.Attribute):
            node = node.value
        return node.id if isinstance(node, ast.Name) else None

    def test_no_mutating_or_exec_calls(self) -> None:
        # Scoped to filesystem modules on purpose: ``str.replace`` on a path
        # string is not a filesystem mutation, ``os.replace`` is.
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in self.FORBIDDEN_ATTRS:
                    self.assertNotIn(
                        self._root_name(node.func),
                        self.MUTATING_MODULES,
                        f"mutating call {self._root_name(node.func)}.{node.func.attr}()",
                    )
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, self.FORBIDDEN_NAMES)

    def test_only_open_call_is_binary_read_mode(self) -> None:
        opens = [
            node for node in ast.walk(self._tree())
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "open"
        ]
        self.assertTrue(opens, "expected exactly one read-only open() call")
        for call in opens:
            self.assertGreaterEqual(len(call.args), 2, "open() must pass mode")
            self.assertIsInstance(call.args[1], ast.Constant)
            self.assertEqual(call.args[1].value, "rb")
            self.assertFalse(call.keywords, "open() must not use keyword modes")

    def test_no_write_valued_mode_literal(self) -> None:
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertNotIn(node.value, {"w", "a", "x", "r+", "w+", "a+"})

    def test_extra_write_parameters_refused(self) -> None:
        # extra="forbid" on ToolBase: a caller cannot smuggle a content/dest arg.
        with self.assertRaises(Exception):
            ReadFileTool(file_path="a.txt", content="overwrite")


if __name__ == "__main__":
    unittest.main()
