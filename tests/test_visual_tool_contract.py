"""
Contract tests for ``tools/visual_tool.py`` (the ``visual`` grain's only tool).

The tool has three independent contract surfaces, each pinned here:

1. **The permission contract.**  ``required_categories`` is what the security
   gate is asked about.  Two rules make it fragile, so both are pinned against
   the real gate (``security.security_gate.check_required_categories``) rather
   than against a mock:
   - an entry WITHOUT a ``':'`` is skipped outright
     (``security/security_gate.py:1568-1570``: ``if ':' not in req: continue
     # malformed, skip``), so a bare ``"visual"`` would grant nothing while
     *looking* like a requirement;
   - a requirement is only worth its level: ``visual:view`` must be present,
     not a bare ``visual`` or ``visual:true``.
2. **Fail-closed admission.**  No container may be created when no render image
   is provisioned, or when the configured one is not on the admission
   allow-list.  The fake manager used by those tests RAISES if constructed, so
   "refused before any container work" is asserted, not assumed.
3. **Lifecycle and the target guard.**  The render container is ephemeral, is
   never healed, is removed on every path (including a propagating exception),
   and a teardown failure never masks the tool's result.  ``_resolve_target``
   confines rendering to ``/workspace`` with a distinct message per escape
   route.

4. **The host-write gate.**  The rendered PNG is written by the HOST process,
   not by the render container, so it needs a write-capable ``filesystem``
   grain of its own.  ``filesystem:write`` is declared in
   ``required_categories`` AND re-checked inside ``execute()``, immediately
   before the first host-side mutation: the outer gate ran before
   ``execute()``, so a grant swapped in between must not ride through.
   Read-only, banned, ``ask``, missing and unresolved grants all refuse, and a
   gate that raises refuses too -- with no container created and no artifact
   directory made.

Style: mock-based and hermetic (no Docker daemon, no render image, no network),
mirroring ``tests/security/test_git_info_tool_operations.py`` (a recording
fake manager monkeypatched onto the tool module, no live container) and
``tests/test_resource_catalog.py`` (catalog/manifest parity read straight from
disk).
"""

import base64
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

# ``tools`` must be imported BEFORE ``security.security_gate``: see
# ``tests/security/test_gate_import_order.py`` for the order-dependent import
# chain these two packages share.  Keep this import above the gate import.
import tools as tools_pkg  # noqa: E402,F401  (order-sensitive; see above)

# Add project root so that imports work (same pattern as sibling tests).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402  (import order forced by the sys.path shim)

import tools.visual_tool as visual_tool  # noqa: E402
from security.security_gate import check_required_categories  # noqa: E402
from session.tool_presets import AGENT_TOOLS, _ALL_TOOLS  # noqa: E402
from thoughtmachine.container_record import LIFECYCLE_EPHEMERAL  # noqa: E402
from tools.visual_tool import (  # noqa: E402
    RENDER_IMAGE_UNAVAILABLE,
    VisualTargetError,
    VisualTool,
    _resolve_target,
)

#: A minimal, structurally valid PNG (signature + filler).  The tool verifies
#: the signature, never the image, so filler bytes are enough.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"render" * 8

#: Grant set that both gate surfaces need for a render to complete: the two
#: grains the outer gate checks (``visual:view``, ``container:true``) plus the
#: host-write grain (``filesystem:write``) the in-``execute()`` re-check reads
#: from ``effective_permissions``.
WRITE_GRANTS = {"visual": "view", "container": True, "filesystem": "write"}

#: Image reference the fake "provisioned" build admits; the repo ships
#: ``VISUAL_RENDER_IMAGE = None``, so every execution test has to provision one.
FAKE_RENDER_IMAGE = "render-image:v1"
FAKE_ALLOWLIST = ("render-image",)


# ---------------------------------------------------------------------------
# Fakes (mirror tests/security/test_git_info_tool_operations.py: a recording
# ContainerManager stand-in, monkeypatched onto the tool module)
# ---------------------------------------------------------------------------
class _FakeManager:
    """Recording ``ContainerManager`` stand-in.

    Records ``start``/``exec``/``remove`` instead of touching Docker, and can be
    told to RAISE if constructed (the fail-closed tests need exactly that: they
    assert no container work happens at all).
    """

    instances = []
    raise_on_init = False
    remove_error = None

    def __init__(self, **kwargs):
        if _FakeManager.raise_on_init:
            raise AssertionError(
                "ContainerManager was constructed on a fail-closed path"
            )
        self.kwargs = kwargs
        self.start_calls = []
        self.exec_calls = []
        self.remove_calls = []
        _FakeManager.instances.append(self)

    def start(self, **kwargs):
        self.start_calls.append(kwargs)
        return {"id": "render-container-1"}

    def exec(self, container_id, command="", **kwargs):
        self.exec_calls.append((container_id, command, kwargs))
        if command.startswith("base64 "):
            return {
                "exit_code": 0,
                "stdout": base64.b64encode(PNG_BYTES).decode("ascii"),
                "stderr": "",
            }
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    def remove(self, container_id):
        self.remove_calls.append(container_id)
        if _FakeManager.remove_error is not None:
            raise _FakeManager.remove_error


@pytest.fixture
def fake_manager(monkeypatch):
    """Replace the tool's ContainerManager with the recording fake."""
    _FakeManager.instances.clear()
    _FakeManager.raise_on_init = False
    _FakeManager.remove_error = None
    monkeypatch.setattr(visual_tool, "ContainerManager", _FakeManager)
    return _FakeManager


@pytest.fixture
def provisioned_image(monkeypatch):
    """Provision a render image the admission allow-list admits."""
    monkeypatch.setattr(visual_tool, "VISUAL_RENDER_IMAGE", FAKE_RENDER_IMAGE)
    monkeypatch.setattr(visual_tool, "ADMISSION_IMAGE_ALLOWLIST", FAKE_ALLOWLIST)


@pytest.fixture
def hermetic_ids(monkeypatch):
    """Keep workspace-id and worker-name resolution off the vault."""
    monkeypatch.setattr(visual_tool, "_resolve_workspace_id", lambda workspace: None)
    monkeypatch.setattr(visual_tool, "current_worker_name", lambda: None)


@pytest.fixture
def workspace(tmp_path):
    """A throwaway workspace holding one renderable file."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "page.html").write_text("<html>hi</html>", encoding="utf-8")
    return ws


def _tool(workspace, target="file:///workspace/page.html", **params):
    """Construct a VisualTool bound to ``workspace`` (registry fallback path)."""
    return VisualTool(target=target, workspace_path=str(workspace), **params)


def _result(out):
    """Parse the tool's JSON envelope."""
    return json.loads(out)


# ---------------------------------------------------------------------------
# 1. Permission contract (against the real gate)
# ---------------------------------------------------------------------------
def test_bare_visual_requirement_is_skipped_by_the_gate():
    """A colonless requirement grants NOTHING -- the trap this tool avoids.

    ``check_required_categories`` skips any entry without a ``':'``
    (security/security_gate.py:1568-1570), so a required list of ``["visual"]``
    would return ``(True, "")`` even for a session with ``visual: banned``.
    The tool must therefore name ``visual:view`` exactly, and every one of its
    requirements must carry a level.
    """
    allowed, message = check_required_categories(
        ["visual"], {"visual": "banned", "container": True}, "VisualTool", {}, "d"
    )
    assert (allowed, message) == (True, ""), "the bare-'visual' trap moved"

    # ...and the tool never relies on that trap.
    assert VisualTool.required_categories == [
        "visual:view",
        "container:true",
        "filesystem:write",
    ]
    for requirement in VisualTool.required_categories:
        assert ":" in requirement, f"malformed requirement would be skipped: {requirement!r}"


def test_banned_visual_denies_and_names_visual_view():
    """``visual: banned`` denies, and the denial names the missing grain."""
    allowed, message = check_required_categories(
        VisualTool.required_categories,
        {"visual": "banned", "container": True},
        "VisualTool",
        {},
        "Screenshot one workspace file.",
    )
    assert allowed is False
    assert "visual:view" in message
    assert "visual:banned" in message


def test_worker_context_ask_on_visual_is_denied():
    """POST-R1 truth: ``visual: ask`` is DENIED without an interactive user.

    ``view`` now ranks at 2.5, strictly above ``_ASK_SILENT_MAX_RANK`` (2 =
    max(banned, read, ask)), so ``_value_satisfies("view", "ask")`` returns the
    ``"ASK"`` sentinel -- not ``True`` -- and the prompt/worker-deny block at
    ``security/security_gate.py:1614-1626`` IS entered.  Consequences pinned
    here:

    * worker context: denied (no interactive user to prompt);
    * non-worker context with no event bus: denied identically, because there
      is nobody to answer the prompt either (the block fires on
      ``event_bus is None``);
    * so ``visual: ask`` is no longer indistinguishable from ``visual: view``:
      ``ask`` really asks.  ``view`` stays a silent allow (control below).

    The control case below proves the worker-deny block itself is not
    visual-specific: an ``ask`` session whose requirement ranks ABOVE the
    silent tier (``network:write``) is refused the same way.
    """
    permitted = {"visual": "ask", "container": True, "filesystem": "write"}

    for kwargs in ({"is_worker_context": True}, {}):
        allowed, message = check_required_categories(
            VisualTool.required_categories, dict(permitted), "VisualTool", {}, "d",
            **kwargs,
        )
        assert allowed is False, f"visual:ask must not pass silently ({kwargs})"
        assert "ask requires interactive approval" in message
        assert "visual:view" in message, "the denial must name the pending ask"

    # Control (i): ``view`` is still a silent allow in worker context.
    assert check_required_categories(
        VisualTool.required_categories, dict(WRITE_GRANTS),
        "VisualTool", {}, "d", is_worker_context=True,
    ) == (True, "")

    # Control (ii): same ``ask`` session value, a requirement above the
    # silent tier -- the worker-deny block is alive independently of ``visual``.
    allowed, message = check_required_categories(
        ["network:write"], {"network": "ask"}, "VisualTool", {}, "d",
        is_worker_context=True,
    )
    assert allowed is False
    assert "ask requires interactive approval" in message


def test_view_container_and_filesystem_write_allow():
    """The documented working session: the three grains the tool really needs."""
    assert check_required_categories(
        VisualTool.required_categories,
        dict(WRITE_GRANTS),
        "VisualTool",
        {},
        "d",
    ) == (True, "")


def test_get_required_categories_ignores_params():
    """The requirement set is static: params can never widen or narrow it."""
    expected = ["visual:view", "container:true", "filesystem:write"]
    assert VisualTool.get_required_categories({}) == expected
    assert VisualTool.get_required_categories(None) == expected


# ---------------------------------------------------------------------------
# 2. Fail-closed image admission (no container may be constructed)
# ---------------------------------------------------------------------------
def test_fail_closed_when_no_render_image_is_provisioned(monkeypatch, workspace):
    """(a) Nothing provisioned -> refusal, and NO container is constructed."""
    _FakeManager.instances.clear()
    _FakeManager.raise_on_init = True
    monkeypatch.setattr(visual_tool, "ContainerManager", _FakeManager)
    monkeypatch.setattr(visual_tool, "VISUAL_RENDER_IMAGE", None)

    result = _result(_tool(workspace).execute())

    assert result["success"] is False
    assert result["error"] == RENDER_IMAGE_UNAVAILABLE
    assert _FakeManager.instances == []
    assert not (workspace / "artifacts").exists()


def test_fail_closed_when_render_image_is_not_admitted(monkeypatch, workspace):
    """(b) Provisioned but not allow-listed -> the same refusal, same silence."""
    _FakeManager.instances.clear()
    _FakeManager.raise_on_init = True
    monkeypatch.setattr(visual_tool, "ContainerManager", _FakeManager)
    monkeypatch.setattr(visual_tool, "VISUAL_RENDER_IMAGE", FAKE_RENDER_IMAGE)
    monkeypatch.setattr(visual_tool, "ADMISSION_IMAGE_ALLOWLIST", ("other-image",))

    result = _result(_tool(workspace).execute())

    assert result["success"] is False
    assert result["error"] == RENDER_IMAGE_UNAVAILABLE
    assert _FakeManager.instances == []


def test_fail_closed_when_the_lazy_admission_import_breaks(monkeypatch, workspace):
    """(c) The lazy import fails -> refusal, never ``None`` ("proceed").

    ``_image_repo_name`` is imported *inside* ``_render_image_refusal`` to break
    the security_gate -> tools -> visual_tool -> admission_gate cycle, so the
    import has to be a guard and not a bypass: if it raises, the reference
    cannot be proven admitted and the answer is the D2 refusal.
    """
    _FakeManager.instances.clear()
    _FakeManager.raise_on_init = True
    monkeypatch.setattr(visual_tool, "ContainerManager", _FakeManager)
    monkeypatch.setattr(visual_tool, "VISUAL_RENDER_IMAGE", FAKE_RENDER_IMAGE)
    monkeypatch.setattr(visual_tool, "ADMISSION_IMAGE_ALLOWLIST", FAKE_ALLOWLIST)
    # ``None`` in sys.modules makes ``from security.admission_gate import ...``
    # raise ImportError -- the exact failure the lazy import has to absorb.
    monkeypatch.setitem(sys.modules, "security.admission_gate", None)

    assert visual_tool._render_image_refusal() == RENDER_IMAGE_UNAVAILABLE

    result = _result(_tool(workspace).execute())

    assert result["success"] is False
    assert result["error"] == RENDER_IMAGE_UNAVAILABLE
    assert _FakeManager.instances == []
    assert not (workspace / "artifacts").exists()


# ---------------------------------------------------------------------------
# 3. Target guard
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "target, expected",
    [
        ("", "'target' is required"),
        ("https://example.com/page.html", "https:// targets are not supported"),
        ("ftp://example.com/page.html", "unsupported target"),
        ("file:///etc/passwd", "target must be inside the workspace"),
        ("file:///workspace/%2e%2e/secret.txt", "percent-encoded characters"),
        ("file:///workspace/../secret.txt", "no '..' segments"),
        ("file:///workspace/missing.html", "no regular file"),
    ],
)
def test_target_guard_refuses_each_escape_route(workspace, target, expected):
    """Seven refused shapes, each with its own actionable message."""
    with pytest.raises(VisualTargetError) as excinfo:
        _resolve_target(target, str(workspace))
    assert expected in str(excinfo.value)


def test_http_and_escape_refusals_are_distinct(workspace):
    """A remote URL and a traversal attempt must NOT share a message.

    The model is told what to fix; collapsing the two into one string would
    make it retry the wrong thing.
    """

    def refusal(target):
        with pytest.raises(VisualTargetError) as excinfo:
            _resolve_target(target, str(workspace))
        return str(excinfo.value)

    http_message = refusal("https://example.com/page.html")
    escape_message = refusal("file:///workspace/../secret.txt")

    assert http_message != escape_message
    assert "not supported" in http_message and "network" in http_message
    assert "not supported" not in escape_message
    assert "no '..' segments" in escape_message


# ---------------------------------------------------------------------------
# 4. Lifecycle, teardown and start() arguments
# ---------------------------------------------------------------------------
def test_teardown_runs_when_an_exception_escapes(
    monkeypatch, workspace, fake_manager, provisioned_image, hermetic_ids
):
    """A propagating exception still removes the container, exactly once."""
    def _boom(*args, **kwargs):
        raise RuntimeError("argv exploded")

    monkeypatch.setattr(visual_tool, "_render_argv", _boom)

    with pytest.raises(RuntimeError, match="argv exploded"):
        _tool(workspace, effective_permissions=dict(WRITE_GRANTS)).execute()

    assert len(_FakeManager.instances) == 1
    assert _FakeManager.instances[0].remove_calls == ["render-container-1"]


def test_teardown_failure_does_not_mask_the_result(
    monkeypatch, workspace, fake_manager, provisioned_image, hermetic_ids, caplog
):
    """A failing ``remove`` is logged, never allowed to replace the result."""
    monkeypatch.setattr(
        _FakeManager, "remove_error", RuntimeError("docker went away")
    )

    with caplog.at_level(logging.WARNING, logger="tools.visual_tool"):
        result = _result(
            _tool(workspace, effective_permissions=dict(WRITE_GRANTS)).execute()
        )

    assert result["success"] is True
    assert result["mime_type"] == "image/png"
    assert len(_FakeManager.instances[0].remove_calls) == 1
    assert any(
        "could not remove render container" in record.getMessage()
        for record in caplog.records
    )


def test_start_receives_ephemeral_lifecycle_and_no_heal(
    workspace, fake_manager, provisioned_image, hermetic_ids
):
    """The render container is ephemeral, never healed, and pinned to the image."""
    result = _result(
        _tool(
            workspace,
            worker_name="w-1",
            effective_permissions=dict(WRITE_GRANTS),
        ).execute()
    )

    assert result["success"] is True
    manager = _FakeManager.instances[0]
    start = manager.start_calls[0]
    assert start["lifecycle_class"] == LIFECYCLE_EPHEMERAL
    assert start["heal_missing"] is False
    assert start["image"] == FAKE_RENDER_IMAGE
    assert start["worker_name"] == "w-1"
    # The manager itself is bound to the same image, workspace and grant set.
    assert manager.kwargs["image"] == FAKE_RENDER_IMAGE
    assert manager.kwargs["workspace_path"] == str(workspace)
    assert manager.remove_calls == ["render-container-1"]


def test_render_container_is_memory_capped(
    workspace, fake_manager, provisioned_image, hermetic_ids
):
    """The untrusted renderer is constructed with the operator 512 MB cap.

    ``ContainerManager`` already defaults ``mem_limit`` to
    ``DEFAULT_MEM_LIMIT`` (``"512m"``), so *inheriting* it looks capped --
    but then the render cap is simply whatever the product-wide default
    happens to be, and a change made for some unrelated container silently
    re-sizes the one container that runs untrusted third-party code.  The
    values are therefore passed explicitly at the call site, and this test
    pins the concrete numbers rather than ``is not None``: deleting the
    kwargs, or drifting either constant, must fail here.
    """
    result = _result(
        _tool(workspace, effective_permissions=dict(WRITE_GRANTS)).execute()
    )

    assert result["success"] is True
    manager = _FakeManager.instances[0]
    assert manager.kwargs["mem_limit"] == "512m"
    assert manager.kwargs["cpu_quota"] == 100000


# ---------------------------------------------------------------------------
# 5. The host-write gate: filesystem:write, re-checked before the host write
# ---------------------------------------------------------------------------
def test_host_write_gate_allows_a_write_capable_filesystem_grant(
    workspace, fake_manager, provisioned_image, hermetic_ids
):
    """(a) ``filesystem: write`` -> the rendered PNG is written to the workspace."""
    result = _result(
        _tool(workspace, effective_permissions=dict(WRITE_GRANTS)).execute()
    )

    assert result["success"] is True
    written = workspace / result["artifact_path"]
    assert written.exists(), "success must mean the artifact is really on disk"
    assert written.read_bytes() == PNG_BYTES
    assert result["bytes"] == len(PNG_BYTES)


@pytest.mark.parametrize("filesystem_grant", ["read", "banned", "ask", "none", None, "MISSING"])
def test_host_write_gate_refuses_every_non_write_grant(
    workspace, fake_manager, provisioned_image, hermetic_ids, filesystem_grant
):
    """(b,c,d) read/banned/ask/None/missing all refuse before any container.

    ``ask`` is refused on purpose: the in-tool atomic re-check treats a not
    pre-approved escalation as DENIED (``check_atomic_operation``), and there
    is no user to prompt this late in the call anyway.  ``None`` (present but
    unset) and a MISSING key are both fail-closed, so a session whose
    ``filesystem`` grain was never resolved can never write.
    """
    fake_manager.raise_on_init = True
    grants = dict(WRITE_GRANTS)
    if filesystem_grant == "MISSING":
        grants.pop("filesystem")
    else:
        grants["filesystem"] = filesystem_grant

    result = _result(_tool(workspace, effective_permissions=grants).execute())

    assert result["success"] is False
    assert "filesystem:write" in result["error"], result["error"]
    assert "effective filesystem grant" in result["error"], result["error"]
    assert "No container was created" in result["error"], result["error"]
    # (d) the refusal precedes ALL container work, and creates no directory.
    assert fake_manager.instances == []
    assert not (workspace / "artifacts").exists()


def test_host_write_gate_fails_closed_without_effective_permissions(
    workspace, fake_manager, provisioned_image, hermetic_ids
):
    """An unresolved ``effective_permissions`` refuses; it never means "allow"."""
    fake_manager.raise_on_init = True

    result = _result(_tool(workspace).execute())

    assert result["success"] is False
    assert "filesystem:write" in result["error"]
    assert fake_manager.instances == []
    assert not (workspace / "artifacts").exists()


def test_host_write_gate_refuses_when_the_gate_itself_raises(
    monkeypatch, workspace, fake_manager, provisioned_image, hermetic_ids
):
    """A gate that cannot be consulted is a refusal, not a bypass."""
    import security.security_gate as gate

    def _boom(*args, **kwargs):
        raise RuntimeError("gate unavailable")

    monkeypatch.setattr(gate, "check_atomic_operation", _boom)
    fake_manager.raise_on_init = True

    result = _result(
        _tool(workspace, effective_permissions=dict(WRITE_GRANTS)).execute()
    )

    assert result["success"] is False
    assert "filesystem:write" in result["error"]
    assert fake_manager.instances == []


def test_required_categories_pin_the_host_write_grain():
    """(e) ``filesystem:write`` is declared, level-qualified and enforced."""
    assert "filesystem:write" in VisualTool.required_categories
    assert VisualTool.required_categories.count("filesystem:write") == 1

    # The declaration is only worth something if the gate can parse it AND the
    # level is ``write``: a bare "filesystem" is skipped outright, and
    # "filesystem:read" would admit a read-only session to the host write.
    allowed, message = check_required_categories(
        ["filesystem:write"], {"filesystem": "read"}, "VisualTool", {}, "d"
    )
    assert allowed is False
    assert "filesystem" in message
    assert check_required_categories(
        ["filesystem:write"], {"filesystem": "write"}, "VisualTool", {}, "d"
    ) == (True, "")


# ---------------------------------------------------------------------------
# 6. Registration surfaces
# ---------------------------------------------------------------------------
def test_catalog_and_manifest_visual_entry_agree():
    """The shipped ``visual`` metadata in B (catalog) and D (manifest) agree.

    ``tools`` must list exactly this tool: the catalog entry is what the UI and
    the endpoint publish, and the manifest's ``safe_default`` is what a vault
    gets seeded with, so a divergence silently advertises the wrong tool set.
    """
    import agent.config.resource_catalog as rc

    config_dir = Path(rc.__file__).resolve().parent
    catalog = json.loads((config_dir / "resource_catalog.json").read_text(encoding="utf-8"))
    manifest = json.loads((config_dir / "schema_manifest.json").read_text(encoding="utf-8"))

    entries = [entry for entry in catalog if entry.get("name") == "visual"]
    assert len(entries) == 1, "exactly one 'visual' entry must exist in the catalog"
    entry = entries[0]
    assert entry["tools"] == ["VisualTool"]
    assert entry["permission_grain_set"] == ["banned", "ask", "view"]
    assert entry["execution_mode"] == "container"

    seed = [
        item
        for item in manifest["files"]["system/resource_catalog.json"]["safe_default"]
        if item.get("name") == "visual"
    ]
    assert seed == [entry]


def test_visual_tool_is_registered_in_every_tool_list():
    """TOOL_CLASSES + get_tool_class + the agent preset lists all carry it.

    The preset lists are plain data and are checked in-process.  The tool
    REGISTRY is checked in a fresh interpreter on purpose: ``TOOL_CLASSES`` is
    import-order dependent (``tests/security/test_gate_import_order.py`` is the
    regression suite for that chain, and it uses the same subprocess idiom).
    Importing ``tools`` first is the order under which the registry is meant to
    be complete; a module that imported ``security.security_gate`` first would
    leave this process's registry in whatever shape THAT chain left it in, which
    is a property of the import graph, not of this tool's registration.
    """
    assert "VisualTool" in _ALL_TOOLS
    assert "VisualTool" in AGENT_TOOLS

    repo_root = Path(__file__).resolve().parents[1]
    script = (
        "import tools\n"
        "from tools.visual_tool import VisualTool\n"
        "names = [cls.__name__ for cls in tools.TOOL_CLASSES]\n"
        "print('registered=' + ('yes' if 'VisualTool' in names else 'no'))\n"
        "print('lookup=' + ('yes' if tools.get_tool_class('VisualTool') is VisualTool else 'no'))\n"
    )
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Failed to import VisualTool" not in completed.stderr, completed.stderr
    assert "registered=yes" in completed.stdout
    assert "lookup=yes" in completed.stdout
