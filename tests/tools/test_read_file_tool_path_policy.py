"""R1 — ``ReadFile`` must apply the tool-layer path policy, not just containment.

``tools/read_file_tool.py`` used to do its own resolve-then-contain check and
never called ``ToolBase._validate_path``, so the vault compartment and
workspace sensitive-path block lists in ``thoughtmachine/security.py`` were
never applied to it even though every other file tool applied them.

These tests pin the fix recorded as R1 in
``ticket-residual-w3-isolation-findings-not-fixed.md``:

* the policy call actually runs (a vault path is refused by the *block list*,
  which is distinguishable from the containment message), and
* the resolve-then-contained check is still there as the second, narrower gate
  (the ticket's MUST-NOT-TOUCH: add the policy call, do not replace the
  containment).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from thoughtmachine.security import VAULT_BLOCKED_SUBDIRS, VAULT_ROOT
from tools.read_file_tool import ReadFile


def _run(workspace: Path, file_path: str) -> dict:
    """Execute ``ReadFile`` for *file_path* rooted at *workspace*."""
    tool = ReadFile(file_path=file_path, workspace_path=str(workspace))
    return json.loads(tool.execute())


# ══════════════════════════════════════════════════════════════════════════
#  The policy call is applied
# ══════════════════════════════════════════════════════════════════════════


class TestPolicyApplied:
    """Paths the block lists own are refused by the policy, not by accident."""

    def test_ordinary_relative_read_still_works(self, tmp_path: Path):
        (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
        result = _run(tmp_path, "notes.txt")
        assert "error" not in result
        assert result["content"] == "hello"

    def test_absolute_in_workspace_path_still_works(self, tmp_path: Path):
        target = tmp_path / "sub" / "notes.txt"
        target.parent.mkdir()
        target.write_text("hello", encoding="utf-8")
        result = _run(tmp_path, str(target))
        assert "error" not in result
        assert result["content"] == "hello"

    @pytest.mark.parametrize("subdir", sorted(VAULT_BLOCKED_SUBDIRS))
    def test_vault_compartment_path_is_refused_by_the_policy(
        self, tmp_path: Path, subdir: str
    ):
        vault_file = os.path.join(VAULT_ROOT, subdir, "secret.json")
        result = _run(tmp_path, vault_file)
        assert "error" in result
        # The refusal must come from the block list.  Containment would *also*
        # refuse this path, but with a different message — so asserting on the
        # message is what distinguishes "policy applied" from "policy missing".
        assert "escapes the workspace directory" not in result["error"]
        assert subdir in result["error"] or "vault" in result["error"].lower()

    def test_vault_root_file_is_refused_by_the_policy(self, tmp_path: Path):
        result = _run(tmp_path, os.path.join(VAULT_ROOT, "credentials.json"))
        assert "error" in result
        assert "escapes the workspace directory" not in result["error"]
        assert "vault" in result["error"].lower()


# ══════════════════════════════════════════════════════════════════════════
#  Containment is still the second gate
# ══════════════════════════════════════════════════════════════════════════


class TestContainmentStillSecondGate:
    """The resolve-then-contain check was kept, not replaced (MUST-NOT-TORCH)."""

    def test_traversal_out_of_the_workspace_is_refused(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (tmp_path / "outside.txt").write_text("nope", encoding="utf-8")

        result = _run(ws, "../outside.txt")
        assert "error" in result
        assert "outside" in result["error"].lower()

    def test_containment_refuses_when_the_policy_is_a_no_op(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Defence in depth: with the policy call neutralised the containment
        check must still refuse an escaping path."""
        ws = tmp_path / "ws"
        ws.mkdir()
        (tmp_path / "outside.txt").write_text("nope", encoding="utf-8")

        monkeypatch.setattr(
            ReadFile, "_validate_path", lambda self, path: str(ws / path)
        )

        result = _run(ws, "../outside.txt")
        assert "escapes the workspace directory" in result["error"]
