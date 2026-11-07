#!/usr/bin/env python3
"""Force-reap ONE orphan container RECORD (operator escape hatch).

PURPOSE
-------
``sweep_orphan_container_records`` reaps a record only once its bound container
is gone AND the record is past its age / retention window.  This CLI is the
operator's explicit override for the residual case: a record whose container is
long gone but which is still "too young" (or whose retention window was never
going to elapse).

It is a thin wrapper over ``infra.container_manager.reap_container_record`` --
all gating logic lives there (single source of truth).  This module only:

  * resolves a record from ``--record`` (an explicit id) or ``--name`` (the
    workspace-scoped container identity), and
  * maps the primitive's result into a process exit code.

SAFETY
------
Every safety gate the sweeper enforces is retained EXCEPT the AGE gate:
own-lifecycle classes and live-bound containers are refused, and a missing
docker daemon soft-fails.  Without ``--apply`` the run is a read-only dry run.

MODES
-----
``--record ID``   operate on the record with this id.
``--name NAME``   resolve ``NAME`` -> record id in the workspace (refusing when
                  the name is unknown or AMBIGUOUS -- claimed by two records).
``--apply``       actually delete the record and emit the audit trail; omit for
                  a dry run that only reports what would happen.
``--json``        emit a single JSON object instead of ``key=value`` lines.

EXIT CODES
----------
0 -- success: reaped (``--apply``) or a would-reap dry run (``ok`` / ``dry_run``)
2 -- refused: the record was not found (or the name is unknown / ambiguous),
     the record is not reap-able (container still live, own-lifecycle class,
     unknown class), or the ``--record`` / ``--name`` arguments are invalid
     (both given, or neither given)
3 -- infrastructure failure: the docker SDK is unavailable, a delete failed, or
     any unexpected error occurred
"""

import argparse
import json
import os
import sys

# Make the repository root importable when run directly as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from infra.container_manager import ContainerManager, reap_container_record
from thoughtmachine.container_record import list_records

try:  # the docker SDK is the only external dependency ContainerManager() needs
    from docker.errors import DockerException
except ImportError:  # pragma: no cover - the missing-SDK path raises RuntimeError
    DockerException = None

#: Failures while BUILDING a ContainerManager that mean "infrastructure
#: unavailable" (exit 3) -- deliberately narrower than ``except Exception`` so
#: genuine programming errors are not silently swallowed as exit code 3.
_CM_BUILD_ERRORS = (ImportError, RuntimeError)
if DockerException is not None:
    _CM_BUILD_ERRORS += (DockerException,)

#: Primitive ``reason`` -> process exit code.  Any unmapped reason is a
#: conservative 3 (infrastructure failure).
_EXIT_FOR_REASON = {
    "ok": 0,
    "dry_run": 0,
    "record_not_found": 2,
    "container_live": 2,
    "lifecycle_own": 2,
    "unknown_class": 2,
    "docker_unavailable": 3,
    "delete_failed": 3,
}

#: Keys emitted, in order, by the human-readable (non-)--json output.
_RESULT_KEYS = (
    "workspace_id",
    "record_id",
    "docker_id",
    "found",
    "reaped",
    "applied",
    "reason",
    "detail",
)


def _resolve_name(workspace_id, name):
    """Resolve *name* -> record id in *workspace_id*.

    Returns ``(record_id, None)`` on success or ``(None, exit_code)`` when the
    name is unknown / ambiguous (2) or the docker SDK is missing (3).  Reads
    only: the name index is built from ``list_records`` (no docker daemon is
    contacted; ``docker.from_env`` does not ping).
    """
    try:
        cm = ContainerManager(workspace_path=os.getcwd(), workspace_id=workspace_id)
    except _CM_BUILD_ERRORS as exc:
        print(
            f"ERROR: cannot build ContainerManager (docker SDK / daemon unavailable?): "
            f"{exc}",
            file=sys.stderr,
        )
        return None, 3

    cm._ensure_name_index()
    candidates = [r.id for r in list_records(workspace_id) if r.name == name]

    if cm._name_collision(name):
        print(
            f"ERROR: name {name!r} is AMBIGUOUS in workspace "
            f"{workspace_id!r} (claimed by 2+ records); candidates: "
            f"{', '.join(candidates) or '(none)'}",
            file=sys.stderr,
        )
        return None, 2

    record_id = cm._record_for_name(name)
    if not record_id:
        print(
            f"ERROR: no record named {name!r} in workspace {workspace_id!r}; "
            f"candidates: {', '.join(candidates) or '(none)'}",
            file=sys.stderr,
        )
        return None, 2
    return record_id, None


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="reap_container_record",
        description="Force-reap ONE orphan container RECORD, bypassing the AGE "
                    "gate only (the escape hatch for a record the age-gated "
                    "sweeper still considers 'too young').  Without --apply it "
                    "is a read-only dry run.",
        epilog="Exit codes: 0 ok, 2 refused (not found / not reap-able / bad "
               "arguments), 3 infrastructure failure.",
    )
    parser.add_argument(
        "--workspace", required=True, metavar="ID",
        help="Workspace id owning the record (required).",
    )
    parser.add_argument(
        "--record", default=None, metavar="ID",
        help="Record id to force-reap (exactly one of --record / --name).",
    )
    parser.add_argument(
        "--name", default=None, metavar="NAME",
        help="Workspace-scoped container name to resolve to a record id "
             "(exactly one of --record / --name).",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually delete the record and emit audit.  Default: dry run.",
    )
    parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="Emit a single JSON object instead of key=value lines.",
    )
    parser.add_argument(
        "--actor", default="cli",
        help="Attribution recorded on the RECORD_FORCE_REAP audit line "
             "(default: cli).",
    )
    parser.add_argument(
        "--reason", default="",
        help="Free-form reason recorded on the RECORD_FORCE_REAP audit line.",
    )
    args = parser.parse_args(argv)

    # --record / --name are mutually exclusive AND exactly one is required.
    if bool(args.record) == bool(args.name):
        print(
            "ERROR: provide exactly one of --record or --name (not both, not "
            "neither).",
            file=sys.stderr,
        )
        return 2

    workspace_id = args.workspace
    record_id = args.record

    if record_id is None:
        record_id, code = _resolve_name(workspace_id, args.name)
        if code is not None:
            return code

    try:
        result = reap_container_record(
            workspace_id, record_id, apply=args.apply,
            actor=args.actor, reason=args.reason,
        )
    except Exception as exc:  # defensive: the primitive is designed not to raise
        print(f"ERROR: reap_container_record raised: {exc}", file=sys.stderr)
        return 3

    code = _EXIT_FOR_REASON.get(result.get("reason"), 3)

    if args.as_json:
        print(json.dumps({**result, "exit_code": code}, indent=2, sort_keys=True))
    else:
        for key in _RESULT_KEYS:
            print(f"{key}={result.get(key, '')}")

    if code != 0:
        print(
            f"ERROR: reason={result.get('reason', '')} "
            f"detail={result.get('detail', '')}",
            file=sys.stderr,
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
