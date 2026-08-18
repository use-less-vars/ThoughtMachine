"""
Worker -> container ownership bridge (label + context-var + injection).

Contracts under test:

  A. The worker context var (agent.core.worker_context.WORKER_NAME_CONTEXTVAR)
     defaults to None, is stamped with the worker's identity
     ("<session_id>:<worker_name>") for the duration of
     WorkerThread._run_tool_loop, and is reset afterwards.
  B. ToolExecutor injects ``worker_name`` into tools that declare the field
     whenever the context var is set, and leaves it unset otherwise.
  C. ContainerStartTool forwards worker_name to ContainerManager.start: an
     explicit field value wins, otherwise the context var is used.
  D. DockerCodeRunner accepts an optional worker_name field.

No real LLM, no real Docker, no production code changes.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, List, Optional
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.core.tool_executor import ToolExecutor  # noqa: E402
from agent.core.worker_context import (  # noqa: E402
    WORKER_NAME_CONTEXTVAR,
    current_worker_name,
)
from tools.base import ToolBase  # noqa: E402
from tools.container_control import ContainerStartTool  # noqa: E402
from tools.docker_code_runner import DockerCodeRunner  # noqa: E402
from tools.workspace.worker import WorkerThread  # noqa: E402

SYS_PROMPT = "You are a helpful worker assistant."


def make_thread(workspace_dir, name="w-test", timeout=60, session_id="s1"):
    return WorkerThread(
        name=name,
        definition={"system_prompt": SYS_PROMPT},
        agent_config={"model": "gpt-4o"},
        workspace_dir=Path(workspace_dir),
        session_id=session_id,
        timeout_seconds=timeout,
    )


class _FakeEventBus:
    """Callable stand-in for agent.events.EventBus."""

    def __init__(self, *a, **k):
        pass

    def publish(self, *a, **k):
        return None


class _RunSafetyPatches(unittest.TestCase):
    """Patches event plumbing so WorkerThread.run() can execute headlessly."""

    def setUp(self):
        patchers = [
            mock.patch("tools.workspace.worker.EventBus", new=_FakeEventBus),
            mock.patch("tools.workspace.worker.register_worker_event_bus",
                       new=lambda *a, **k: None),
            mock.patch("tools.workspace.worker.unregister_worker_event_bus",
                       new=lambda *a, **k: None),
            mock.patch("tools.workspace.worker.global_event_bus", new=None),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        super().setUp()


class TestWorkerContextVar(_RunSafetyPatches):
    """Context var: default None, stamped during _run_tool_loop, reset after."""

    def test_context_var_defaults_to_none(self):
        self.assertIsNone(current_worker_name())
        self.assertIsNone(WORKER_NAME_CONTEXTVAR.get())

    def test_run_tool_loop_stamps_and_resets_context_var(self):
        class _CtxAwareFakeAgent:
            def __init__(self, seen):
                self.seen = seen
                self.state = SimpleNamespace(
                    time_state=SimpleNamespace(value="LOW"),
                    restriction_reason=None,
                )

            def request_pause(self):
                return None

            def process_query(self, query):
                self.seen["inside"] = current_worker_name()
                yield {"type": "agent_responded", "status": "final",
                       "content": "done"}

        with tempfile.TemporaryDirectory() as tmp:
            seen = {}
            thread = make_thread(tmp, name="w-ctx-bridge")
            # Skip StateBridge/EventProcessor lazy-init -> fully headless.
            thread._state_bridge = SimpleNamespace(context_length=0)
            thread._agent = _CtxAwareFakeAgent(seen)

            result = thread._run_tool_loop("query-1")

            self.assertEqual(result, "done")
            # The stamp carries the worker's identity (session_id + name),
            # not the bare name — RED today: prod stamps only self.worker_name.
            self.assertEqual(seen["inside"], "s1:w-ctx-bridge")
            # The stamp must not leak past the turn.
            self.assertIsNone(current_worker_name())
            self.assertIsNone(WORKER_NAME_CONTEXTVAR.get())


class _WorkerAwareTool(ToolBase):
    """Minimal tool declaring worker_name, mirroring ContainerStartTool."""

    tool: str = "_WorkerAwareTool"
    required_categories: ClassVar[List[str]] = []
    worker_name: Optional[str] = None

    def execute(self) -> str:
        return json.dumps({"worker_name": self.worker_name})


class TestToolExecutorInjection(unittest.TestCase):
    """ToolExecutor injects worker_name from the context var when declared."""

    def _make_executor(self):
        config = SimpleNamespace(
            workspace_path=None,
            tool_output_token_limit=None,
            session_permissions=None,
        )
        return ToolExecutor(
            tool_classes=[_WorkerAwareTool],
            config=config,
            state=SimpleNamespace(),
            logger=None,
        )

    def test_executor_injects_worker_name(self):
        executor = self._make_executor()
        token = WORKER_NAME_CONTEXTVAR.set("s1:w-exec-inject")
        try:
            result = executor._execute_single_tool(
                _WorkerAwareTool, {}, "_WorkerAwareTool", 0,
                lambda: False, lambda: None, lambda: 0,
            )
        finally:
            WORKER_NAME_CONTEXTVAR.reset(token)
        self.assertEqual(result["tool_type"], "normal")
        self.assertEqual(
            json.loads(result["result"])["worker_name"], "s1:w-exec-inject"
        )

    def test_executor_leaves_worker_name_unset_without_context(self):
        executor = self._make_executor()
        self.assertIsNone(current_worker_name())
        result = executor._execute_single_tool(
            _WorkerAwareTool, {}, "_WorkerAwareTool", 0,
            lambda: False, lambda: None, lambda: 0,
        )
        self.assertEqual(result["tool_type"], "normal")
        self.assertIsNone(json.loads(result["result"])["worker_name"])


class TestContainerStartToolWorkerName(unittest.TestCase):
    """ContainerStartTool: explicit worker_name wins, else context var."""

    def _execute_with_fake_manager(self, tool):
        fake_manager = mock.Mock()
        fake_manager.start.return_value = {
            "id": "c1", "name": "n1", "status": "created", "note": None,
        }
        with mock.patch.object(ContainerStartTool, "_make_manager",
                               return_value=fake_manager):
            tool.execute()
        return fake_manager

    def test_container_start_tool_passes_worker_name(self):
        fake_manager = self._execute_with_fake_manager(
            ContainerStartTool(worker_name="s1:w-explicit")
        )
        self.assertEqual(
            fake_manager.start.call_args.kwargs["worker_name"], "s1:w-explicit"
        )

    def test_container_start_tool_falls_back_to_context_var(self):
        token = WORKER_NAME_CONTEXTVAR.set("s1:w-ctx-fallback")
        try:
            fake_manager = self._execute_with_fake_manager(ContainerStartTool())
        finally:
            WORKER_NAME_CONTEXTVAR.reset(token)
        self.assertEqual(
            fake_manager.start.call_args.kwargs["worker_name"], "s1:w-ctx-fallback"
        )

    def test_container_start_tool_no_worker_name_no_context(self):
        self.assertIsNone(current_worker_name())
        fake_manager = self._execute_with_fake_manager(ContainerStartTool())
        self.assertIsNone(fake_manager.start.call_args.kwargs["worker_name"])


class TestDockerCodeRunnerWorkerName(unittest.TestCase):
    """DockerCodeRunner accepts (and defaults) the worker_name field."""

    def test_docker_code_runner_accepts_worker_name(self):
        runner = DockerCodeRunner(command="echo hi", worker_name="s1:w-runner")
        self.assertEqual(runner.worker_name, "s1:w-runner")

    def test_docker_code_runner_defaults_to_none(self):
        runner = DockerCodeRunner(command="echo hi")
        self.assertIsNone(runner.worker_name)


if __name__ == "__main__":
    unittest.main()
