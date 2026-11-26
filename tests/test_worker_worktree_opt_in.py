"""A per-session worktree for a worker is OPT-IN, not a default.

``WorkerThread._build_agent_config`` used to call
``resolve_worker_worktree_root`` and set ``worktree_root`` to
``<workspace>/.worktrees/<session>`` whenever the parent session had none.  A
worker confined to that directory could neither see nor commit the tree the
agent's git tools and the docker mount use, so its edits became a copy that no
diff, commit or CI run ever saw.

Pinned here:

  * the default config carries NO ``worktree_root`` and runs on the registered
    workspace root (``workspace_path``) -- asserted on the kwargs the worker
    hands to ``AgentConfig``, not merely on the resulting model;
  * exactly one INFO names the root the worker's file tools will use, so an
    operator can see where their edits land;
  * isolation is still reachable: an explicitly configured ``worktree_root``
    survives untouched, and no "registered workspace root" receipt is logged
    for it.

Hermetic: real ``WorkerThread`` objects against pytest ``tmp_path``; no git, no
container, no network, and the query loop is never entered.
"""

from __future__ import annotations

import logging
from typing import List

import pytest

from agent.config.models import AgentConfig
from tools.workspace.worker_thread import WorkerThread

SESSION = "sess-worktree-opt-in"
WORKER = "opt-in-worker"
PARENT_CFG = {"provider": "openai", "model": "gpt-4o"}

#: Where ``_build_agent_config`` resolves the symbol: it imports ``AgentConfig``
#: lazily (``from agent.config.models import AgentConfig``), so the defining
#: module's attribute is what is read at call time.
AGENT_CONFIG = "agent.config.models.AgentConfig"

#: The one receipt the default path must emit.
RECEIPT = "Worker file root: registered workspace root"


@pytest.fixture
def project_root(tmp_path):
    """The registered workspace root a worker runs in by default."""
    root = tmp_path / "project-root"
    root.mkdir()
    return root


def _thread(tmp_path, project_root, agent_config=None):
    return WorkerThread(
        name=WORKER,
        definition={"system_prompt": "opt-in fixture"},
        agent_config=dict(agent_config if agent_config is not None else PARENT_CFG),
        workspace_dir=tmp_path,
        session_permissions={},
        project_root=str(project_root),
        session_id=SESSION,
    )


def _receipts(caplog) -> List[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == "tools.workspace.worker"
        and RECEIPT in record.getMessage()
    ]


def test_config_carries_no_worktree_root(tmp_path, project_root, monkeypatch):
    """The kwargs handed to ``AgentConfig`` contain no worktree root at all."""
    recorded: List[dict] = []

    class _RecordingAgentConfig(AgentConfig):
        """Real ``AgentConfig`` that records the kwargs it was built with."""

        def __init__(self, **kwargs):
            recorded.append(dict(kwargs))
            super().__init__(**kwargs)

    monkeypatch.setattr(AGENT_CONFIG, _RecordingAgentConfig)
    thread = _thread(tmp_path, project_root)

    cfg = thread._build_agent_config()

    assert cfg is not None, "the config build must never be skipped"
    assert recorded, "the worker never handed a config to AgentConfig"
    kwargs = recorded[-1]
    assert kwargs.get("worktree_root") is None, (
        f"spawning must not invent a worktree_root; got {kwargs.get('worktree_root')!r}"
    )
    assert kwargs["workspace_path"] == str(project_root)
    # ... and the built model agrees: the tree the git tools and the docker
    # mount see, not a per-session checkout.
    assert cfg.worktree_root is None
    assert cfg.workspace_path == str(project_root)


def test_default_path_logs_one_receipt_for_the_workspace_root(
    tmp_path, project_root, caplog
):
    """One INFO per spawn names the root the worker's file tools will use."""
    thread = _thread(tmp_path, project_root)

    with caplog.at_level(logging.INFO, logger="tools.workspace.worker"):
        cfg = thread._build_agent_config()

    assert cfg is not None
    receipts = _receipts(caplog)
    assert len(receipts) == 1, (
        "expected exactly one file-root receipt per spawn; got "
        f"{[r.getMessage() for r in receipts]}"
    )
    assert receipts[0].levelno == logging.INFO
    text = receipts[0].getMessage()
    assert str(project_root) in text, (
        f"the receipt must name the root actually used; got {text!r}"
    )
    assert "set worktree_root to isolate" in text, (
        f"the receipt must point at the opt-in; got {text!r}"
    )


def test_explicit_worktree_root_is_preserved_and_not_logged(
    tmp_path, project_root, caplog
):
    """Opting in still works: the caller's root survives and is not overridden."""
    isolated = tmp_path / "isolated-tree"
    isolated.mkdir()
    thread = _thread(
        tmp_path,
        project_root,
        agent_config={**PARENT_CFG, "worktree_root": str(isolated)},
    )

    with caplog.at_level(logging.INFO, logger="tools.workspace.worker"):
        cfg = thread._build_agent_config()

    assert cfg is not None
    assert cfg.worktree_root == str(isolated)
    assert cfg.workspace_path == str(project_root)
    assert not _receipts(caplog), (
        "an explicitly isolated worker must not log the default file root; got "
        f"{[r.getMessage() for r in _receipts(caplog)]}"
    )


if __name__ == "__main__":  # pragma: no cover - manual runs
    raise SystemExit(pytest.main([__file__]))
