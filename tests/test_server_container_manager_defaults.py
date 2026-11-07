"""The web route's ``ContainerManager`` must use the resource-defaults SSOT.

``_make_container_manager`` used to omit ``mem_limit``/``cpu_quota`` and rely on
``ContainerManager.__init__``'s literals, which is exactly how the HTTP route and
the agent tool drifted apart.  The route now states its limits explicitly from
``agent.config.defaults``, so the two can never disagree again.
"""
from unittest.mock import patch


def _server_module():
    import web_ui.backend.server as server  # lazy: needs sys.path

    return server


def test_make_container_manager_passes_resource_defaults(monkeypatch):
    from agent.config.defaults import DEFAULT_CPU_QUOTA, DEFAULT_MEM_LIMIT

    server = _server_module()
    monkeypatch.setattr(
        server, "_resolve_workspace_path", lambda wid, wp="": "/tmp/ws"
    )
    with patch("infra.container_manager.ContainerManager") as fake_cls:
        server._make_container_manager("ws-1", "/tmp/ws")

    kwargs = fake_cls.call_args.kwargs
    assert kwargs["workspace_id"] == "ws-1"
    assert kwargs["mem_limit"] == DEFAULT_MEM_LIMIT
    assert kwargs["cpu_quota"] == DEFAULT_CPU_QUOTA


def test_unresolvable_workspace_returns_none(monkeypatch):
    server = _server_module()
    monkeypatch.setattr(server, "_resolve_workspace_path", lambda wid, wp="": "")
    with patch("infra.container_manager.ContainerManager") as fake_cls:
        assert server._make_container_manager("ws-missing") is None
    fake_cls.assert_not_called()
