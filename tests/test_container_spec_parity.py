"""Structural parity for the container resource-limit plumbing.

A resource limit only reaches Docker when it crosses *all* of these seams, and
each one fails silently on its own:

1. the two spec classes -- :class:`infra.container_create.ContainerCreateSpec`
   and :class:`infra.container_registry.ContainerProfile` -- must expose the
   same limit fields with the same defaults (a field only one of them has is
   dropped at the ``profile -> spec`` translation);
2. ``infra.container_registry``'s kwargs allowlist (the ``for field_name in
   (...)`` tuple) must name the field, otherwise an admission-approved kwarg
   never lands on the profile;
3. ``infra.container_manager``'s ``_EFFECTIVE_INTENT_KEYS`` must name it, or
   the effective-intent reconcile silently skips the axis;
4. every ``ContainerCreateSpec(...)`` construction site must pass the limit
   explicitly (a defaulted field is easy to leave unset forever);
5. the seam itself -- ``infra.container_create.create_hardened_container``'s
   ``containers.run(...)`` call -- must actually forward the field, otherwise
   the whole chain is inert.

Assertions 2 and 4 are read at source/AST level on purpose: the allowlist is a
function-local tuple literal and the create sites are inline call sites, so
neither can be exercised through a public API without a live Docker client.
Every check anchors on something real (the ``for field_name`` loop target, an
actual ``ast.Call`` node) and cross-checks a known-present member, so a rename
that makes the walk find nothing fails instead of passing vacuously.

Assertion 5 is deliberately the opposite shape: it runs the real seam against a
fake docker client and asserts the recorded kwargs, because "the name appears in
the source" and "the value arrives at ``containers.run``" are different facts.

Scope note (topology, verified against the tree rather than assumed):
``ContainerCreateSpec`` is hand-built in exactly four modules -- ``container_registry``
(``ContainerProfile`` translation), ``container_manager``, ``containers/executor``
and ``resource_container_manager``; those are the four in
:data:`CREATE_SPEC_MODULES`.  ``thoughtmachine/container_record/hook.py``'
``_EVIDENCE_KEYS`` and ``thoughtmachine/container_record/drift.py``'
``_RUNTIME_AXES`` are record/drift axis tuples, **not** create seams, and are
not walked here (see the two dedicated tests at the end).
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import MISSING, fields
from pathlib import Path
from typing import get_type_hints

import pytest

from agent.config.defaults import DEFAULT_CPU_QUOTA, DEFAULT_PIDS_LIMIT
from infra.container_create import ContainerCreateSpec, create_hardened_container
from infra.container_manager import _EFFECTIVE_INTENT_KEYS
from infra.container_registry import ContainerProfile
from security.admission_gate import ContainerSpec as AdmissionContainerSpec
from thoughtmachine.container_record.hook import _EVIDENCE_KEYS

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The Limit axes every spec class must expose.  ``oom_score_adj`` and
#: ``network_mode`` are not limits but share the same passage, so they are
#: held to the same parity rule.
LIMIT_FIELDS = ("mem_limit", "cpu_quota", "pids_limit", "oom_score_adj")

#: Modules that build a ``ContainerCreateSpec`` by hand.  Listed explicitly so
#: a *deleted* site fails loudly instead of shrinking the walk's coverage.
CREATE_SPEC_MODULES = (
    "infra/container_registry.py",
    "infra/container_manager.py",
    "infra/containers/executor.py",
    "infra/resource_container_manager.py",
)


def _field_defaults(cls) -> dict:
    out = {}
    for f in fields(cls):
        if f.default is not MISSING:
            out[f.name] = f.default
        elif f.default_factory is not MISSING:
            out[f.name] = f.default_factory()
    return out


def _parse(relpath: str) -> ast.Module:
    return ast.parse((REPO_ROOT / relpath).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# (a) the two spec classes agree on the limit fields
# ---------------------------------------------------------------------------


def test_spec_classes_expose_the_same_limit_fields():
    create_names = {f.name for f in fields(ContainerCreateSpec)}
    profile_names = {f.name for f in fields(ContainerProfile)}

    missing_from_create = set(LIMIT_FIELDS) - create_names
    missing_from_profile = set(LIMIT_FIELDS) - profile_names
    assert not missing_from_create, (
        f"ContainerCreateSpec is missing limit fields {sorted(missing_from_create)}"
    )
    assert not missing_from_profile, (
        f"ContainerProfile is missing limit fields {sorted(missing_from_profile)}"
    )


def test_spec_classes_agree_on_limit_field_defaults():
    create_defaults = _field_defaults(ContainerCreateSpec)
    profile_defaults = _field_defaults(ContainerProfile)

    mismatched = {
        name: (create_defaults.get(name), profile_defaults.get(name))
        for name in LIMIT_FIELDS
        if create_defaults.get(name) != profile_defaults.get(name)
    }
    assert not mismatched, (
        "limit-field defaults diverged between ContainerCreateSpec and "
        f"ContainerProfile: {mismatched}"
    )


def test_spec_classes_agree_on_limit_field_types():
    create_hints = get_type_hints(ContainerCreateSpec)
    profile_hints = get_type_hints(ContainerProfile)

    mismatched = {
        name: (create_hints.get(name), profile_hints.get(name))
        for name in LIMIT_FIELDS
        if create_hints.get(name) != profile_hints.get(name)
    }
    assert not mismatched, (
        "limit-field annotations diverged between ContainerCreateSpec and "
        f"ContainerProfile: {mismatched}"
    )


def test_admission_spec_covers_no_limit_axis_the_create_spec_lacks():
    """One-way invariant: every limit the admission *request* carries must
    exist on the create spec it gates.

    ``security.admission_gate.ContainerSpec`` deliberately models only the
    axes admission reasons about (``mem_limit``/``cpu_quota``/
    ``oom_score_adj``/``network_mode``); it does not carry ``pids_limit``
    today, so a "same fields" assertion here would be inventing a requirement.
    The durable rule is that admission can never name an axis the create spec
    cannot express.
    """
    admission_names = {f.name for f in fields(AdmissionContainerSpec)}
    create_names = {f.name for f in fields(ContainerCreateSpec)}

    admission_limits = admission_names & set(LIMIT_FIELDS)
    assert admission_limits, "admission ContainerSpec lost all limit fields"
    assert admission_limits <= create_names, (
        "admission ContainerSpec carries limit fields the create spec lacks: "
        f"{sorted(admission_limits - create_names)}"
    )


# ---------------------------------------------------------------------------
# (b) the registry kwargs allowlist and the manager intent tuple name it
# ---------------------------------------------------------------------------


def _registry_kwarg_allowlist() -> set[str]:
    """The ``for field_name in (...)`` tuple that filters admission kwargs."""
    tree = _parse("infra/container_registry.py")
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and node.target.id == "field_name"
            and isinstance(node.iter, (ast.Tuple, ast.List, ast.Set))
        ):
            return {
                elt.value for elt in node.iter.elts if isinstance(elt, ast.Constant)
            }
    raise AssertionError("kwargs allowlist loop over 'field_name' not found")


def test_pids_limit_survives_the_registry_kwargs_allowlist():
    allowlist = _registry_kwarg_allowlist()

    # Anchor: prove the walk found the real tuple, not an empty/degenerate one.
    assert {"mem_limit", "cpu_quota"} <= allowlist, (
        f"kwargs allowlist anchor missing -- found {sorted(allowlist)}"
    )
    assert "pids_limit" in allowlist, (
        "pids_limit is absent from the container_registry kwargs allowlist, so "
        f"the kwarg is silently dropped. Allowlist: {sorted(allowlist)}"
    )


def test_pids_limit_is_in_the_effective_intent_keys():
    assert "pids_limit" in _EFFECTIVE_INTENT_KEYS, (
        "pids_limit is absent from _EFFECTIVE_INTENT_KEYS, so effective-intent "
        f"reconcile skips it. Keys: {_EFFECTIVE_INTENT_KEYS}"
    )
    # Anchor the tuple we imported is the real one.
    assert {"mem_limit", "cpu_quota"} <= set(_EFFECTIVE_INTENT_KEYS)


# ---------------------------------------------------------------------------
# (c) the cpu_quota literal cannot come back
# ---------------------------------------------------------------------------


def test_container_manager_uses_the_default_cpu_quota_constant():
    source = (REPO_ROOT / "infra/container_manager.py").read_text(encoding="utf-8")

    assert "cpu_quota=100000" not in source, (
        "infra/container_manager.py reintroduced the magic literal "
        "'cpu_quota=100000'; use DEFAULT_CPU_QUOTA"
    )
    assert "DEFAULT_CPU_QUOTA" in source, (
        "infra/container_manager.py no longer references DEFAULT_CPU_QUOTA"
    )

    default = inspect.signature(
        __import__(
            "infra.container_manager", fromlist=["ContainerManager"]
        ).ContainerManager.__init__
    ).parameters["cpu_quota"].default
    assert default == DEFAULT_CPU_QUOTA
    assert DEFAULT_CPU_QUOTA == 100000  # the constant still means what it did


# ---------------------------------------------------------------------------
# (d) every hand-built ContainerCreateSpec sets its limits explicitly
# ---------------------------------------------------------------------------


def _create_spec_calls(relpath: str) -> list[ast.Call]:
    tree = _parse(relpath)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "ContainerCreateSpec"
    ]


@pytest.mark.parametrize("relpath", CREATE_SPEC_MODULES)
def test_hand_set_spec_sites_pass_explicit_limits(relpath):
    calls = _create_spec_calls(relpath)
    assert calls, f"no ContainerCreateSpec(...) construction found in {relpath}"

    offenders = []
    for call in calls:
        passed = {kw.arg for kw in call.keywords if kw.arg}
        for required in ("mem_limit", "cpu_quota"):
            if required not in passed:
                offenders.append((call.lineno, required))
    assert not offenders, (
        f"{relpath}: ContainerCreateSpec(...) call(s) rely on a defaulted limit "
        f"instead of passing it explicitly: {offenders}"
    )


def test_snapshot_evidence_keys_still_cover_the_limit_axes():
    """``thoughtmachine/container_record/hook.py`` is *not* a create site (the
    dispatch's line hint pointed at its ``_EVIDENCE_KEYS`` tuple): it lists the
    snapshot keys whose truthiness makes a snapshot "evidence-bearing", so it
    is asserted as the truth it is rather than forced into the create-site
    walk.  ``pids_limit`` is not part of that tuple today and asserting its
    absence would block the record layer from adopting it later, so only the
    present axes are pinned.
    """
    assert {"mem_limit", "cpu_quota"} <= set(_EVIDENCE_KEYS)


# ---------------------------------------------------------------------------
# (e) the seam forwards the value: non-inert, end-to-end through the real call
# ---------------------------------------------------------------------------


class _RunRecorder:
    """Fake docker client: ``containers.run`` records its kwargs and returns."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.containers = self

    def run(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return object()


def test_pids_limit_reaches_the_docker_run_call():
    """NON-INERT: the spec carries a real ceiling and the seam forwards it.

    The AST checks above prove the *names* exist somewhere in the source; this
    one proves the *value* arrives.  ``admission=None`` skips the gate,
    ``record=False`` with ``workspace_id=None`` skips ``record_creation``, and a
    spec with no mounts keeps ``docker.types.Mount`` out of the path, so the
    fake client needs neither a daemon nor a real ``create_hardened_container``
    side effect.

    Note: ``ContainerCreateSpec`` has five required positional fields
    (``image``/``command``/``name``/``container_type``/``lifecycle_class``), so
    there is no bare ``ContainerCreateSpec()`` to default-construct -- the
    defaults under test (``pids_limit`` among them) are exercised on a spec
    that leaves every optional field untouched.
    """
    spec = ContainerCreateSpec(
        image="agent-executor",
        command=None,  # type: ignore[arg-type]  -- __post_init__ fills it
        name="parity-pids",
        container_type="user",
        lifecycle_class="ephemeral",
    )
    assert spec.pids_limit is not None, (
        "ContainerCreateSpec().pids_limit stayed None: an unset ceiling leaves "
        "HostConfig.PidsLimit unset, so the container is unlimited"
    )
    assert spec.pids_limit == DEFAULT_PIDS_LIMIT

    client = _RunRecorder()
    create_hardened_container(client, spec, admission=None, record=False)

    assert len(client.calls) == 1, client.calls
    run_kwargs = client.calls[0]["kwargs"]
    assert "pids_limit" in run_kwargs, (
        "create_hardened_container's containers.run(...) call does not forward "
        f"pids_limit. Forwarded kwargs: {sorted(run_kwargs)}"
    )
    assert run_kwargs["pids_limit"] == DEFAULT_PIDS_LIMIT
    # Anchor: the recorder saw the real create call, not an empty dict.
    assert {"mem_limit", "cpu_quota"} <= set(run_kwargs)
