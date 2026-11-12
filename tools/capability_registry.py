"""Per-spawn capability registry for worker tool sets.

Implements Sections 4.1/4.2 of ``.thoughtmachine/working_docs/design-worker-capability-registry.md``:
the resolved tool set is FROZEN AT SPAWN and INJECTED, so a name outside the set
is not resolvable at all rather than filtered out at runtime.

Nothing global lives here.  The available-name universe AND the name -> class map
are injected by the caller at spawn (``resolve(lookup=..., available_names=...)``);
this module keeps no cache, no singleton and no module-level state, so two
registries built from different subsets are fully independent and a look-up can
never fall through to a process-wide tool map.

Permission evaluation is NOT performed here (Section 7.2): the registry answers
*which tools exist for this instance*, while ``required_categories`` plus the
footprint answer *whether a chosen tool may run*.  The only permission-shaped
input is an opaque ``permission_veto`` callback supplied by the caller, which
this module invokes and never interprets.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Optional, Tuple

__all__ = ["CapabilityRegistry", "CapabilityResolveError"]


class CapabilityResolveError(Exception):
    """Declared tool name(s) are not shippable by this build (Section 4.2).

    Raised at spawn rather than warn-and-continue: a typo or a deleted tool must
    not silently produce a narrower worker, because the narrowing looks like
    success (ticket 4).  ``missing_names`` holds the offending names, sorted.
    """

    def __init__(self, missing_names: Iterable[str]) -> None:
        self.missing_names: Tuple[str, ...] = tuple(sorted(missing_names))
        super().__init__(
            "worker template names tool(s) this build does not ship: "
            + ", ".join(self.missing_names)
        )


@dataclass(frozen=True)
class CapabilityRegistry:
    """The frozen, injected tool set of ONE worker instance.

    Build it with :meth:`resolve`; there is no mutation API at all (Section 4.1:
    a later edit to the template affects only the next spawn).  ``resolved`` is a
    ``MappingProxyType``, so ``registry.resolved[name] = cls`` raises ``TypeError``
    and the set cannot grow after construction.
    """

    resolved: Mapping[str, type]
    names: Tuple[str, ...]
    excluded: Tuple[str, ...]
    unavailable: Tuple[str, ...]
    permission_denied: Tuple[str, ...]
    parent_denied: Tuple[str, ...]
    declared_empty: bool
    implicit: bool

    def __post_init__(self) -> None:
        # Re-seal even when a caller bypasses resolve(): the map is copied into a
        # read-only proxy and every bucket is stored as a tuple, so no later
        # mutation of the caller's objects can reach a constructed registry.
        object.__setattr__(self, "resolved", MappingProxyType(dict(self.resolved)))
        for bucket in (
            "names",
            "excluded",
            "unavailable",
            "permission_denied",
            "parent_denied",
        ):
            object.__setattr__(self, bucket, tuple(getattr(self, bucket)))

    @classmethod
    def resolve(
        cls,
        declared_names: Iterable[str],
        *,
        available_names: Iterable[str],
        lookup: Mapping[str, type],
        blocklist: Iterable[str] = (),
        parent_enabled_tools: Optional[Iterable[str]] = None,
        permission_veto: Optional[Callable[[str], bool]] = None,
    ) -> "CapabilityRegistry":
        """Resolve *declared_names* into a frozen registry instance.

        Rules, applied in this order:

        1. An empty declaration means EVERY available name, minus the blocklist.
           That is the historical meaning of ``tools: []`` and it is preserved
           for one release (Section 4.2 migration) but flagged ``implicit=True``
           so the privilege is reported, never silent.
        2. A non-empty declaration means exactly those names.
        3. Names in *blocklist* are removed and recorded in ``.excluded``.  The
           blocklist is additive to the subset, never replaced by it (Section 4.2).
        4. Names this build does not ship -- absent from *available_names*, or
           present there but unresolvable through *lookup* -- are recorded in
           ``.unavailable`` and raise :class:`CapabilityResolveError`, naming them
           (loud failure at spawn).
        5. The set is intersected with *parent_enabled_tools* when not ``None``:
           a worker is never wider than its parent.
        6. ``permission_veto(name)`` returning ``True`` moves the name to
           ``.permission_denied``.  Soft: the worker is built without it, no raise,
           because the operator may legitimately hold a smaller grant than the
           template assumed.

        Names removed by rule 5 land in ``.parent_denied``: neither the template
        nor the build refused them, the parent did, and Section 7.2 point 3
        requires every narrowing step to be visible in the audit record.
        ``.names`` is always sorted, so the audit payload is stable.
        """
        declared = tuple(dict.fromkeys(declared_names))
        available = frozenset(available_names)
        blocked = frozenset(blocklist)

        declared_empty = not declared
        requested = available if declared_empty else frozenset(declared)

        candidates = sorted(name for name in (available if declared_empty else requested) if name not in blocked)
        excluded = tuple(sorted(name for name in requested if name in blocked))

        unavailable = tuple(
            name for name in candidates if name not in available or lookup.get(name) is None
        )
        if unavailable:
            raise CapabilityResolveError(unavailable)

        parent_denied: Tuple[str, ...] = ()
        if parent_enabled_tools is not None:
            parent = frozenset(parent_enabled_tools)
            parent_denied = tuple(
                sorted(name for name in candidates if name not in parent)
            )
            candidates = [name for name in candidates if name in parent]

        denied = []
        kept = []
        for name in candidates:
            if permission_veto is not None and permission_veto(name):
                denied.append(name)
                continue
            kept.append(name)

        names = tuple(sorted(kept))
        return cls(
            resolved={name: lookup[name] for name in names},
            names=names,
            excluded=excluded,
            unavailable=unavailable,
            permission_denied=tuple(sorted(denied)),
            parent_denied=parent_denied,
            declared_empty=declared_empty,
            implicit=declared_empty,
        )

    def get(self, name: str) -> Optional[type]:
        """The class granted for *name*, or ``None``.

        A miss is final: there is no fallback to a global registry, because this
        registry is the only source of truth for this instance (Section 4.2).
        """
        return self.resolved.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self.resolved

    def __len__(self) -> int:
        return len(self.names)

    def describe(self) -> dict:
        """Audit payload for the spawn record (Section 7.2 point 3)."""
        return {
            "size": len(self.names),
            "names": list(self.names),
            "excluded": list(self.excluded),
            "permission_denied": list(self.permission_denied),
            "parent_denied": list(self.parent_denied),
            "implicit": self.implicit,
            "source": "implicit" if self.implicit else "explicit",
        }
