"""Opt-in, real-daemon proof that the restart policy actually takes effect.

This is the end-to-end counterpart to the hermetic restart-policy tests in
``tests/test_container_restart_policy.py``: those prove the policy NAME is
threaded into ``containers.run(...)``; this proves the daemon *restarts* a
container -- created with the very policy our own code emits -- after its main
process exits non-zero on its own.

STIMULUS.  The container's command is ``sh -c "sleep 2; exit 1"``: it exits by
itself.  This is deliberate.  An earlier revision SIGKILLed PID 1 with
``container.kill()``; on at least one real engine ``kill()`` is treated as an
explicit stop, and ``unless-stopped`` must NOT restart an explicitly stopped
container -- so that stimulus could never prove anything.  A self-exit removes
all dependence on how the engine interprets ``kill``.

CONTROL.  If our emitted policy does not restart the container we run a second,
*deliberately hardcoded* ``always`` control with the same self-exiting command.
Only if the control restarts does this become a real failure (our value did not
take effect); if neither restarts, the daemon simply does not restart
self-exited containers here and we SKIP.

It talks to a REAL Docker daemon and therefore SKIPS CLEANLY when:
  * the ``docker`` SDK is not importable (``pytest.importorskip``), or
  * the daemon is unreachable (``client.ping()`` raises -> ``pytest.skip``), or
  * the ``alpine`` image is unavailable and cannot be pulled, or
  * the opt-in env var ``TM_RUN_DOCKER_RESTART_TESTS=1`` is not set.

It NEVER fails merely because no daemon is present -- that is the normal CI
sandbox state.  Engines also differ in WHERE (if anywhere) they expose restart
evidence, so every read uses ``.get`` (never ``[]``) and the live test turns an
evidence-free engine into a SKIP carrying a rich diagnostic rather than a
``KeyError``/failure.

The pure helpers at module scope are exercised by ordinary, daemon-free unit
tests in this same file (the ``test_restart_*`` / ``test_state_*`` /
``test_decide_*`` functions); those are NEVER opt-in gated, so the logic stays
hermetically verifiable even where no daemon exists.

Only ``thoughtmachine.container_record`` is imported -- never
``infra.container_manager``, which has a circular-import cascade at collection
time (see ``tests/docker/test_container_lifecycle.py``).
"""

import copy
import os
import time

import pytest

docker = pytest.importorskip("docker")

from thoughtmachine.container_record import (  # noqa: E402  (after importorskip)
    LIFECYCLE_PERSISTENT,
    docker_restart_policy,
)

_IMAGE = "alpine:latest"

#: Opt-in gate (repo convention for live-daemon proofs).
_OPT_IN_ENV = "TM_RUN_DOCKER_RESTART_TESTS"

#: Deliberately self-exiting probe: exits non-zero on its own after ~2 s, so
#: the daemon must restart it under the policy (no ``kill()`` involved).
_SELF_EXIT_CMD = ["sh", "-c", "sleep 2; exit 1"]

#: The hardcoded CONTROL policy -- a deliberate literal (see module docstring).
_CONTROL_POLICY = {"Name": "always", "MaximumRetryCount": 0}

#: Poll the daemon for up to ~45 s (0.5 s interval) after the self-exit.
_POLL_ATTEMPTS = 90
_POLL_INTERVAL_S = 0.5


# --------------------------------------------------------------------------- #
# Pure, engine-agnostic helpers (unit-tested below, no daemon required).
# --------------------------------------------------------------------------- #

def _restart_count(attrs):
    """Return a container's restart count if the engine exposes it, else None.

    Engines differ in WHERE (if anywhere) they surface the restart count: the
    canonical Docker inspect payload nests it at ``State.RestartCount``, but
    some engines expose it at the top level -- and some expose it nowhere.  Read
    every known location with ``.get`` (never ``[]``) and fall back to ``None``
    so a caller can never be aborted by a ``KeyError``.
    """
    if not isinstance(attrs, dict):
        return None
    state = attrs.get("State")
    if isinstance(state, dict):
        value = state.get("RestartCount")
        if value is not None:
            return value
    return attrs.get("RestartCount")


def _started_at(attrs):
    """Return ``State.StartedAt`` from an inspect mapping, or ``None``.

    ``StartedAt`` is the engine-independent fallback evidence of a restart: it
    advances every time the daemon (re)starts the container's main process.
    """
    if not isinstance(attrs, dict):
        return None
    state = attrs.get("State")
    if not isinstance(state, dict):
        return None
    return state.get("StartedAt")


def _restarting(attrs):
    """Return ``State.Restarting`` (bool) if readable, else ``None``."""
    if not isinstance(attrs, dict):
        return None
    state = attrs.get("State")
    if not isinstance(state, dict):
        return None
    value = state.get("Restarting")
    return value if isinstance(value, bool) else None


def _recorded_policy_name(attrs):
    """Return ``HostConfig.RestartPolicy.Name`` via ``.get`` only, or ``None``."""
    if not isinstance(attrs, dict):
        return None
    host_config = attrs.get("HostConfig")
    if not isinstance(host_config, dict):
        return None
    policy = host_config.get("RestartPolicy")
    if not isinstance(policy, dict):
        return None
    return policy.get("Name")


def _restart_observed(before, after):
    """Decide whether *after* demonstrates a restart relative to *before*.

    Engine-independent lines of evidence are accepted (logical OR):

      * ``State.Restarting`` is true; or
      * the **restart count increased**, when the count is readable in BOTH
        snapshots (a readable count in only one snapshot is not evidence); or
      * **``State.StartedAt`` advanced** -- present and different in both
        snapshots -- the fallback for engines that omit ``RestartCount`` (the
        engine behind the original ``KeyError: 'RestartCount'``).

    Returns ``True`` when a restart is demonstrated, ``False`` when evidence is
    available and shows no restart, and ``None`` when NO evidence is available
    at all -- letting the caller SKIP (not fail) on an evidence-free engine.
    Never raises.
    """
    if _restarting(after) is True:
        return True

    evidence_seen = False

    count_before = _restart_count(before)
    count_after = _restart_count(after)
    if count_before is not None and count_after is not None:
        evidence_seen = True
        if count_after > count_before:
            return True

    start_before = _started_at(before)
    start_after = _started_at(after)
    if start_before is not None and start_after is not None:
        evidence_seen = True
        if start_after != start_before:
            return True

    if evidence_seen:
        return False
    return None


def _state_snapshot(attrs):
    """Return a small, engine-agnostic dict of the restart-relevant fields.

    Every read uses ``.get`` (never ``[]``), so an engine that omits a field --
    or returns a non-dict/absent ``State`` -- still yields a report instead of a
    ``KeyError``.  Missing values are ``None``.
    """
    state = attrs.get("State") if isinstance(attrs, dict) else None
    if not isinstance(state, dict):
        state = {}
    return {
        "restart_count": _restart_count(attrs),
        "StartedAt": state.get("StartedAt"),
        "FinishedAt": state.get("FinishedAt"),
        "Status": state.get("Status"),
        "ExitCode": state.get("ExitCode"),
        "Running": state.get("Running"),
        "Restarting": state.get("Restarting"),
        "Pid": state.get("Pid"),
        "restart_policy_name": _recorded_policy_name(attrs),
        "State_keys": sorted(state.keys()),
    }


def _format_snapshot(attrs):
    """One-line human-readable rendering of :func:`_state_snapshot`."""
    snap = _state_snapshot(attrs)
    return " ".join(f"{key}={snap[key]!r}" for key in snap)


def _decide_outcome(our_before, our_after, control_before=None, control_after=None):
    """Classify the live proof from the observed snapshots.

    Returns one of:

      * ``"pass"`` -- our emitted policy restarted the container (normal path).
      * ``"fail"`` -- our policy did NOT restart it, but the hardcoded
        ``always`` CONTROL did: our emitted value is not taking effect on this
        engine (the only genuine-failure branch).
      * ``"skip"`` -- neither restarted (this daemon does not restart
        self-exited containers under any policy), or no control was run.
    """
    if _restart_observed(our_before, our_after) is True:
        return "pass"
    if control_before is None and control_after is None:
        return "skip"
    if _restart_observed(control_before, control_after) is True:
        return "fail"
    return "skip"


# --------------------------------------------------------------------------- #
# Live-daemon plumbing.
# --------------------------------------------------------------------------- #

def _client():
    """Return a pinged Docker client, or skip when unreachable."""
    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:  # daemon absent / socket unreachable
        pytest.skip(f"Docker daemon unreachable: {exc}")
    return client


def _ensure_image(client):
    """Make the test image available, or skip when it cannot be obtained."""
    try:
        client.images.get(_IMAGE)
        return
    except docker.errors.ImageNotFound:
        pass
    except Exception as exc:  # pragma: no cover - daemon flakiness
        pytest.skip(f"could not inspect image {_IMAGE}: {exc}")
    try:
        client.images.pull(_IMAGE)
    except Exception as exc:
        pytest.skip(f"could not pull image {_IMAGE}: {exc}")


def _snapshot(container):
    """Deep-copy the container's inspect attrs (safe across ``reload()``)."""
    try:
        return copy.deepcopy(container.attrs)
    except Exception:
        return None


def _run_probe(client, restart_policy):
    """Run the self-exiting probe image under *restart_policy* (detached)."""
    return client.containers.run(
        _IMAGE,
        _SELF_EXIT_CMD,
        detach=True,
        restart_policy=restart_policy,
    )


def _observe_restart(container):
    """Snapshot *container*, then poll until a restart is observed.

    Returns ``(before, after, restarted)``: ``before`` is taken immediately
    (before the self-exit), ``after`` is the last readable snapshot, and
    ``restarted`` is the final :func:`_restart_observed` verdict.  Reload
    exceptions are tolerated (transient daemon hiccups).
    """
    before = _snapshot(container)
    after = before
    restarted = False
    for _ in range(_POLL_ATTEMPTS):
        time.sleep(_POLL_INTERVAL_S)
        try:
            container.reload()
        except Exception:  # pragma: no cover - transient daemon hiccup
            continue
        after = _snapshot(container)
        if _restart_observed(before, after) is True:
            restarted = True
            break
    return before, after, restarted


def test_restart_policy_takes_effect_on_live_daemon():
    """A self-exited container is restarted by the daemon under OUR policy."""
    # Opt-in gate (live test ONLY; the hermetic helper tests above are never
    # gated).  Ordered BEFORE the ping guard so a developer can see the gate.
    if os.environ.get(_OPT_IN_ENV) != "1":
        pytest.skip(
            "live restart proof: set TM_RUN_DOCKER_RESTART_TESTS=1 to enable"
        )

    client = _client()
    _ensure_image(client)

    requested = docker_restart_policy(LIFECYCLE_PERSISTENT)
    assert requested is not None, (
        "persistent containers must carry a restart policy; "
        "docker_restart_policy returned None"
    )
    expected_name = requested["Name"]

    ours = None
    control = None
    try:
        # --- the container under test: OUR emitted policy ------------------
        ours = _run_probe(client, requested)

        # End-to-end feature check: the daemon recorded OUR requested policy.
        # Every lookup uses ``.get`` so an engine that omits any key cannot
        # abort the test with a ``KeyError``.
        ours.reload()
        recorded = _recorded_policy_name(ours.attrs)
        assert recorded == expected_name, (
            f"daemon recorded restart policy {recorded!r}, expected "
            f"{expected_name!r}"
        )

        our_before, our_after, our_restarted = _observe_restart(ours)
        if our_restarted:
            return

        # --- CONTROL: does the daemon restart self-exited containers at all? -
        control = _run_probe(client, dict(_CONTROL_POLICY))
        ctrl_before, ctrl_after, ctrl_restarted = _observe_restart(control)

        outcome = _decide_outcome(our_before, our_after, ctrl_before, ctrl_after)
        if outcome == "pass":
            return

        our_diag = (
            f"our[{expected_name!r}]: "
            f"before({_format_snapshot(our_before)}) "
            f"after({_format_snapshot(our_after)})"
        )
        ctrl_diag = (
            f"control['always']: "
            f"before({_format_snapshot(ctrl_before)}) "
            f"after({_format_snapshot(ctrl_after)})"
        )

        if outcome == "fail":
            pytest.fail(
                "our emitted restart policy did not take effect while the "
                "hardcoded 'always' control DID restart the self-exited "
                f"container. {our_diag} | {ctrl_diag}"
            )
        pytest.skip(
            "daemon under test does not restart self-exited containers under "
            "ANY policy (our policy AND the hardcoded 'always' control both "
            "failed to restart within the timeout); the live proof is "
            f"impossible on this engine -- not a failure. {our_diag} | {ctrl_diag}"
        )
    finally:
        for container in (ours, control):
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    pass


# --------------------------------------------------------------------------- #
# Hermetic unit tests for the pure helpers (run with no daemon, never gated).
# --------------------------------------------------------------------------- #

def test_restart_count_reads_nested_state():
    assert _restart_count({"State": {"RestartCount": 3}}) == 3
    assert _restart_count({"State": {"RestartCount": 0}}) == 0


def test_restart_count_reads_top_level():
    assert _restart_count({"RestartCount": 2}) == 2


def test_restart_count_absent_is_none():
    assert _restart_count({}) is None
    assert _restart_count({"State": {}}) is None
    assert _restart_count({"State": None}) is None


def test_restart_predicate_false_when_nothing_changed():
    same = {"State": {"RestartCount": 1, "StartedAt": "2026-01-01T00:00:00Z"}}
    assert _restart_observed(same, copy.deepcopy(same)) is False
    # No count anywhere, identical StartedAt -> evidence present, no restart.
    assert (
        _restart_observed(
            {"State": {"StartedAt": "T"}}, {"State": {"StartedAt": "T"}}
        )
        is False
    )


def test_restart_predicate_true_on_count_started_at_or_restarting():
    # Count increase, nested form.
    assert (
        _restart_observed(
            {"State": {"RestartCount": 0}}, {"State": {"RestartCount": 1}}
        )
        is True
    )
    # Count increase, top-level form.
    assert _restart_observed({"RestartCount": 2}, {"RestartCount": 3}) is True
    # StartedAt advanced, no count anywhere (engine-independent fallback).
    assert (
        _restart_observed(
            {"State": {"StartedAt": "2026-01-01T00:00:00Z"}},
            {"State": {"StartedAt": "2026-01-01T00:00:05Z"}},
        )
        is True
    )
    # Engine exposes only ``State.Restarting``.
    assert (
        _restart_observed(
            {"State": {"Restarting": False}}, {"State": {"Restarting": True}}
        )
        is True
    )


def test_restart_predicate_none_when_no_evidence():
    assert _restart_observed({}, {}) is None
    assert _restart_observed({"State": {}}, {"State": {}}) is None
    assert _restart_observed({"State": None}, {"RestartCount": None}) is None
    # Non-dict inputs must be None-safe, never raise.
    assert _restart_observed(None, None) is None


def test_state_snapshot_handles_odd_state():
    # ``State`` lacking RestartCount -> count read from the top level, and the
    # recorded policy name is still surfaced; no KeyError anywhere.
    snap = _state_snapshot(
        {
            "State": {"Status": "exited", "StartedAt": "T0"},
            "RestartCount": 5,
            "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
        }
    )
    assert snap["restart_count"] == 5
    assert snap["Status"] == "exited"
    assert snap["StartedAt"] == "T0"
    assert snap["restart_policy_name"] == "unless-stopped"
    assert snap["State_keys"] == ["StartedAt", "Status"]
    # ``State`` None / absent -> all None, empty key list, still a report.
    for attrs in ({"State": None}, {}):
        snap = _state_snapshot(attrs)
        assert snap["restart_count"] is None
        assert snap["StartedAt"] is None
        assert snap["restart_policy_name"] is None
        assert snap["State_keys"] == []
    # Non-dict input must not raise, and the formatter still renders a string.
    assert _state_snapshot(None)["restart_count"] is None
    assert isinstance(_format_snapshot(None), str)


def test_decide_outcome_branches():
    restarted = {"State": {"RestartCount": 1}}
    not_restarted = {"State": {"RestartCount": 0}}
    # (1) our policy restarted -> pass.
    assert _decide_outcome(not_restarted, restarted) == "pass"
    # (2) neither restarted -> skip (daemon restarts nothing here).
    assert (
        _decide_outcome(not_restarted, not_restarted, not_restarted, not_restarted)
        == "skip"
    )
    # (3) our policy did NOT restart but the control DID -> fail.
    assert (
        _decide_outcome(not_restarted, not_restarted, not_restarted, restarted)
        == "fail"
    )
    # No control run and no restart -> skip (never a false failure).
    assert _decide_outcome(not_restarted, not_restarted) == "skip"
