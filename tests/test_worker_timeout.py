"""Worker timeouts are session-owned with a single terminal module default.

Precedence (highest first): explicit ``timeout_seconds`` param on
WorkerThread, definition ``timeout_seconds``, agent_config
``worker_timeout_seconds``, session_config ``worker_timeout_seconds``,
then the module constant ``WORKER_TIMEOUT_SECONDS`` (600). WorkerManager
passes each thread's resolved ``_timeout_seconds`` to the delivery call.
"""

import inspect
from pathlib import Path
from unittest import mock

from agent.config.defaults import WORKER_TIMEOUT_SECONDS
from tools.workspace.worker_manager import WorkerManager
from tools.workspace.worker_thread import WorkerThread


def _definition(**overrides):
    base = {
        'name': 'test-worker',
        'description': 'test definition',
        'system_prompt': 'you are a test',
        'tools': [],
        'permission_footprint': {},
    }
    base.update(overrides)
    return base


def _thread(tmp_path, definition=None, agent_config=None, timeout_seconds=None):
    return WorkerThread(
        name='test-worker',
        definition=definition or _definition(),
        agent_config=agent_config or {},
        workspace_dir=Path(tmp_path),
        timeout_seconds=timeout_seconds,
    )


class TestWorkerTimeoutOwnedBySession:
    def test_session_config_beats_module_default(self, tmp_path):
        t = _thread(
            tmp_path, agent_config={'session_config': {'worker_timeout_seconds': 42}}
        )
        assert t._timeout_seconds == 42

    def test_agent_config_beats_session_config(self, tmp_path):
        t = _thread(
            tmp_path,
            agent_config={
                'worker_timeout_seconds': 7,
                'session_config': {'worker_timeout_seconds': 42},
            },
        )
        assert t._timeout_seconds == 7

    def test_definition_beats_agent_config(self, tmp_path):
        t = _thread(
            tmp_path,
            definition=_definition(timeout_seconds=99),
            agent_config={'worker_timeout_seconds': 7},
        )
        assert t._timeout_seconds == 99

    def test_explicit_param_beats_definition(self, tmp_path):
        t = _thread(
            tmp_path, definition=_definition(timeout_seconds=99), timeout_seconds=5
        )
        assert t._timeout_seconds == 5

    def test_empty_everything_resolves_to_module_default(self, tmp_path):
        t = _thread(tmp_path)
        assert t._timeout_seconds == WORKER_TIMEOUT_SECONDS
        assert t._timeout_seconds == 600

    def test_non_dict_session_config_is_ignored(self, tmp_path):
        t = _thread(tmp_path, agent_config={'session_config': 'not-a-dict'})
        assert t._timeout_seconds == WORKER_TIMEOUT_SECONDS


class _FakeRegistry:
    """Dict-backed stand-in for the worker registry interface."""

    def __init__(self):
        self._workers = {}

    def register_worker(self, session_id, worker_name, thread, instance_id=1):
        self._workers[(session_id or '', worker_name, instance_id)] = thread

    def unregister_worker(self, session_id, worker_name, instance_id=1, default=None):
        return self._workers.pop((session_id or '', worker_name, instance_id), default)

    def get_worker(self, session_id, worker_name, instance_id=1, default=None):
        return self._workers.get((session_id or '', worker_name, instance_id), default)

    def get_all_workers(self):
        return dict(self._workers)

    def find_workers_by_name(self, worker_name):
        return [(key, thread) for key, thread in self._workers.items() if key[1] == worker_name]


class _FakeThread:
    worker_name = 'w'
    instance_id = 1
    context_tag = None
    _timeout_seconds = 123

    def is_alive(self):
        return True


class TestWorkerManagerReadsSessionTimeout:
    def test_reuse_branch_propagates_thread_timeout(self):
        registry = _FakeRegistry()
        thread = _FakeThread()
        registry.register_worker('s1', 'w', thread)
        mgr = WorkerManager(registry=registry)
        with mock.patch(
            'tools.workspace.worker_manager.deliver_query_and_block',
            return_value={'ok': True},
        ) as deliver:
            envelope = mgr.request_worker('s1', 'q', context_preference={'worker_name': 'w'})
        deliver.assert_called_once()
        kwargs = deliver.call_args.kwargs
        assert kwargs['timeout'] == 123
        assert envelope['delivery']['reused'] is True

    def test_spawn_branch_propagates_thread_timeout(self):
        registry = _FakeRegistry()
        mgr = WorkerManager(registry=registry)
        with mock.patch(
            'tools.workspace.worker_manager.deliver_query_and_block',
            return_value={'ok': True},
        ) as deliver:
            envelope = mgr.request_worker('s1', 'q', spawner=lambda: _FakeThread())
        deliver.assert_called_once()
        kwargs = deliver.call_args.kwargs
        assert kwargs['timeout'] == 123
        assert envelope['delivery']['spawned'] is True

    def test_explicit_timeout_wins_over_thread_timeout(self):
        registry = _FakeRegistry()
        thread = _FakeThread()
        registry.register_worker('s1', 'w', thread)
        mgr = WorkerManager(registry=registry)
        with mock.patch(
            'tools.workspace.worker_manager.deliver_query_and_block',
            return_value={'ok': True},
        ) as deliver:
            envelope = mgr.request_worker(
                's1', 'q', context_preference={'worker_name': 'w'}, timeout=999
            )
        kwargs = deliver.call_args.kwargs
        assert kwargs['timeout'] == 999


class TestNoSilentFallbackForMissingTimeout:
    def test_module_default_is_the_terminal_hop(self, tmp_path):
        from agent.config import defaults

        assert WORKER_TIMEOUT_SECONDS == 600
        assert defaults.WORKER_TIMEOUT_SECONDS == 600
        t = _thread(tmp_path)
        assert t._timeout_seconds == 600

    def test_wait_for_worker_exit_lands_on_constant(self):
        from tools.workspace.worker_timeout import wait_for_worker_exit

        src = inspect.getsource(wait_for_worker_exit)
        assert 'WORKER_TIMEOUT_SECONDS' in src


def test_worker_timeout_owned_by_session(tmp_path):
    """Contract wrapper: worker timeout precedence is session-owned."""
    tc = TestWorkerTimeoutOwnedBySession()
    tc.test_session_config_beats_module_default(tmp_path)
    tc.test_agent_config_beats_session_config(tmp_path)
    tc.test_definition_beats_agent_config(tmp_path)
    tc.test_explicit_param_beats_definition(tmp_path)
    tc.test_empty_everything_resolves_to_module_default(tmp_path)
    tc.test_non_dict_session_config_is_ignored(tmp_path)


def test_worker_manager_reads_session_timeout():
    """Contract wrapper: WorkerManager propagates thread timeout to delivery."""
    tc = TestWorkerManagerReadsSessionTimeout()
    tc.test_reuse_branch_propagates_thread_timeout()
    tc.test_spawn_branch_propagates_thread_timeout()
    tc.test_explicit_timeout_wins_over_thread_timeout()


def test_no_silent_fallback_for_missing_timeout(tmp_path):
    """Contract wrapper: missing timeout lands on the module constant."""
    tc = TestNoSilentFallbackForMissingTimeout()
    tc.test_module_default_is_the_terminal_hop(tmp_path)
    tc.test_wait_for_worker_exit_lands_on_constant()

