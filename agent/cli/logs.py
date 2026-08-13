#!/usr/bin/env python3
"""
tm-logs — query the ThoughtMachine structured lifecycle log streams.

Streams are JSONL files under the canonical vault log directory::

    THOUGHTMACHINE_VAULT_ROOT/logs          (when THOUGHTMACHINE_VAULT_ROOT is set)
    ~/.thoughtmachine/logs                  (default)

    session.log          session lifecycle events      (log_session_event)
    worker_<name>.log    per-worker lifecycle events   (log_worker_event)
    container.log        container lifecycle events    (log_container_event)
    provider_raw.jsonl   raw provider responses        (log_provider_event)

Subcommands::

    tm-logs session [FILTERS] [--format table|json|human]
    tm-logs worker --worker-name NAME [FILTERS] [--format ...]
    tm-logs container [FILTERS] [--format ...]
    tm-logs stop-reasons [FILTERS] [--stop-reason REASON] [--format ...]

Filters (--since, --until, --session-id, --level, --event-type) are shared:
--since/--until compare against the record ``timestamp`` envelope field
(ISO-8601, naive values assumed UTC); the others are exact, case-insensitive
matches against ``level`` / ``event`` / ``session_id``.

Filter applicability (documented design decision): filters that are
meaningless for a subcommand are NOT silently ignored — argparse rejects
them with a clear error (exit code 2):

  * ``--worker-name`` exists only on the ``worker`` subcommand (it selects
    the stream file and is required there).  Anywhere else it is an
    "unrecognized arguments" error.
  * ``--stop-reason`` exists only on the ``stop-reasons`` subcommand
    (provider records are the only ones carrying finish/stop reasons).
    Anywhere else it is an "unrecognized arguments" error.

Missing stream file: a friendly message is printed to stderr and the CLI
exits 0, so scripts/pipelines do not break when a stream has not been
written yet.

The log root is resolved at runtime from ``THOUGHTMACHINE_VAULT_ROOT``
(mirroring ``agent.logging.lifecycle``'s canonical logic) so the CLI honors
the variable however it is invoked.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Dict, List, Tuple

# ---------------------------------------------------------------------------
# canonical log-root + filename resolution (mirrors agent.logging.lifecycle)
# ---------------------------------------------------------------------------


def _safe_name(name: str) -> str:
    """Sanitize a name for use in a file name (mirrors lifecycle._safe_name)."""
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", str(name or ""))


def _log_root() -> str:
    """Resolve the canonical vault log directory at runtime."""
    root = os.environ.get("THOUGHTMACHINE_VAULT_ROOT")
    if root:
        return os.path.join(root, "logs")
    return os.path.join(os.path.expanduser("~"), ".thoughtmachine", "logs")


_STREAM_FILES = {
    "session": "session.log",
    "worker": None,  # computed from --worker-name
    "container": "container.log",
    "stop-reasons": "provider_raw.jsonl",
}

# ---------------------------------------------------------------------------
# timestamp handling
# ---------------------------------------------------------------------------


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp; naive values are assumed UTC."""
    s = value.strip()
    if s[-1:] in ("Z", "z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iso_arg(value: str) -> str:
    """argparse type: accept only parseable ISO-8601 timestamps."""
    try:
        _parse_iso(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid ISO-8601 timestamp: {value!r}")
    return value


# ---------------------------------------------------------------------------
# reading + filtering
# ---------------------------------------------------------------------------


def _read_records(path: str) -> Tuple[List[dict], int]:
    """Read all JSON objects from a JSONL file (skips malformed lines)."""
    records: List[dict] = []
    skipped = 0
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if isinstance(rec, dict):
                records.append(rec)
            else:
                skipped += 1
    return records, skipped


def _matches(rec: dict, args) -> bool:
    """Apply the shared filters to a single record."""
    ts = rec.get("timestamp")
    if args.since is not None:
        try:
            if _parse_iso(str(ts)) < _parse_iso(args.since):
                return False
        except ValueError:
            return False  # unparseable timestamp + time filter => excluded
    if args.until is not None:
        try:
            if _parse_iso(str(ts)) > _parse_iso(args.until):
                return False
        except ValueError:
            return False
    if args.level is not None:
        if str(rec.get("level", "")).lower() != args.level.lower():
            return False
    if args.event_type is not None:
        if str(rec.get("event", "")).lower() != args.event_type.lower():
            return False
    if args.session_id is not None:
        if str(rec.get("session_id", "")) != args.session_id:
            return False
    stop_reason = getattr(args, "stop_reason", None)
    if stop_reason is not None:
        reasons = (str(rec.get("finish_reason", "")), str(rec.get("stop_reason", "")))
        if stop_reason.lower() not in reasons:
            return False
    return True


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

_COLUMNS = {
    "session": ("EVENT", "TIMESTAMP", "LEVEL", "SESSION_ID", "WORKSPACE_ID"),
    "worker": ("EVENT", "TIMESTAMP", "LEVEL", "WORKER_NAME", "WORKER_ID", "SESSION_ID"),
    "container": ("EVENT", "TIMESTAMP", "LEVEL", "CONTAINER_ID", "SESSION_ID", "WORKSPACE_ID"),
}


def _cell(value) -> str:
    """Stringify a table cell, collapsing whitespace so alignment holds."""
    if value is None:
        return ""
    s = str(value).replace("\n", " ").replace("\t", " ")
    return re.sub(r"\s{2,}", " ", s)


def _print_table(rows: List[Tuple[str, ...]], headers: Tuple[str, ...]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    sep = "  "
    print(sep.join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print(sep.join("-" * w for w in widths))
    for row in rows:
        print(sep.join(_cell(c).ljust(widths[i]) for i, c in enumerate(row)))


def _row_for(subcommand: str, rec: dict) -> Tuple[str, ...]:
    if subcommand == "session":
        return (
            str(rec.get("event", "")), str(rec.get("timestamp", "")),
            str(rec.get("level", "")), str(rec.get("session_id", "")),
            str(rec.get("workspace_id", "")),
        )
    if subcommand == "worker":
        return (
            str(rec.get("event", "")), str(rec.get("timestamp", "")),
            str(rec.get("level", "")), str(rec.get("worker_name", "")),
            str(rec.get("worker_id", "")), str(rec.get("session_id", "")),
        )
    return (
        str(rec.get("event", "")), str(rec.get("timestamp", "")),
        str(rec.get("level", "")), str(rec.get("container_id", "")),
        str(rec.get("session_id", "")), str(rec.get("workspace_id", "")),
    )


def _human_line(subcommand: str, rec: dict) -> str:
    ts = rec.get("timestamp", "")
    level = str(rec.get("level", "")).ljust(5)
    event = str(rec.get("event", ""))
    if subcommand == "session":
        return (
            f"{ts} {level} session/{event} "
            f"session_id={rec.get('session_id') or '-'} "
            f"workspace_id={rec.get('workspace_id') or '-'}"
        )
    if subcommand == "worker":
        return (
            f"{ts} {level} worker/{event} "
            f"name={rec.get('worker_name') or '-'} "
            f"worker_id={rec.get('worker_id') or '-'} "
            f"session_id={rec.get('session_id') or '-'}"
        )
    return (
        f"{ts} {level} container/{event} "
        f"container_id={rec.get('container_id') or '-'} "
        f"session_id={rec.get('session_id') or '-'} "
        f"workspace_id={rec.get('workspace_id') or '-'}"
    )


def _render_records(records: List[dict], args, subcommand: str) -> None:
    if args.format == "json":
        print(json.dumps(records, indent=2, ensure_ascii=False))
        return
    if not records:
        print("(no matching records)")
        return
    if args.format == "table":
        rows = [_row_for(subcommand, rec) for rec in records]
        _print_table(rows, _COLUMNS[subcommand])
    else:  # human
        for rec in records:
            print(_human_line(subcommand, rec))


def _run_stop_reasons(records: List[dict], args) -> int:
    filtered = [rec for rec in records if _matches(rec, args)]
    finish_counts: Dict[str, int] = {}
    stop_counts: Dict[str, int] = {}
    for rec in filtered:
        fr = str(rec.get("finish_reason", "") or "")
        if fr:
            finish_counts[fr] = finish_counts.get(fr, 0) + 1
        sr = str(rec.get("stop_reason", "") or "")
        if sr:
            stop_counts[sr] = stop_counts.get(sr, 0) + 1

    if args.format == "json":
        print(json.dumps(
            {
                "finish_reason": finish_counts,
                "stop_reason": stop_counts,
                "total": len(filtered),
            },
            indent=2,
            ensure_ascii=False,
        ))
        return 0

    rows = [
        (f"finish_reason {r}", str(c))
        for r, c in sorted(finish_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    rows += [
        (f"stop_reason {r}", str(c))
        for r, c in sorted(stop_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    if not rows and not filtered:
        print("(no matching records)")
        return 0
    rows.append(("TOTAL", str(len(filtered))))
    if args.format == "table":
        _print_table(rows, ("REASON", "COUNT"))
    else:  # human
        for reason, count in rows:
            print(f"{reason:<26} {count}")
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def _add_common_filters(sp: argparse.ArgumentParser) -> None:
    sp.add_argument(
        "--since", type=_iso_arg, metavar="ISO8601",
        help="only records with timestamp >= this time (naive values assumed UTC)",
    )
    sp.add_argument(
        "--until", type=_iso_arg, metavar="ISO8601",
        help="only records with timestamp <= this time (naive values assumed UTC)",
    )
    sp.add_argument(
        "--session-id", metavar="ID",
        help="only records with this session_id",
    )
    sp.add_argument(
        "--level", metavar="LEVEL",
        help="only records with this level (case-insensitive)",
    )
    sp.add_argument(
        "--event-type", metavar="TYPE",
        help="only records with this event type (case-insensitive)",
    )
    sp.add_argument(
        "--format", choices=("table", "json", "human"), default="human",
        help="output format (default: human)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tm-logs",
        description="Query the ThoughtMachine structured lifecycle log streams "
                    "(session.log, worker_<name>.log, container.log, provider_raw.jsonl).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "filter notes:\n"
            "  --worker-name is only valid with the 'worker' subcommand (and required there);\n"
            "  --stop-reason is only valid with the 'stop-reasons' subcommand;\n"
            "  any other filter combination is rejected with an argparse error (exit 2).\n"
            "missing stream file: message on stderr, exit 0."
        ),
    )
    subparsers = parser.add_subparsers(
        dest="subcommand", required=True, metavar="SUBCOMMAND",
        help="session | worker | container | stop-reasons",
    )

    sp = subparsers.add_parser(
        "session", help="read session lifecycle events (session.log)"
    )
    _add_common_filters(sp)
    sp.set_defaults(subcommand="session")

    sp = subparsers.add_parser(
        "worker", help="read a worker's lifecycle events (worker_<name>.log)"
    )
    _add_common_filters(sp)
    sp.add_argument(
        "--worker-name", required=True, metavar="NAME",
        help="worker name selecting worker_<safe name>.log (required)",
    )
    sp.set_defaults(subcommand="worker")

    sp = subparsers.add_parser(
        "container", help="read container lifecycle events (container.log)"
    )
    _add_common_filters(sp)
    sp.set_defaults(subcommand="container")

    sp = subparsers.add_parser(
        "stop-reasons",
        help="aggregate finish_reason / stop_reason counts from provider_raw.jsonl",
    )
    _add_common_filters(sp)
    sp.add_argument(
        "--stop-reason", metavar="REASON",
        help="only count provider records whose finish_reason or stop_reason matches",
    )
    sp.set_defaults(subcommand="stop-reasons")

    return parser


def _run(args, parser: argparse.ArgumentParser) -> int:
    log_dir = _log_root()
    filename = _STREAM_FILES[args.subcommand]
    if args.subcommand == "worker":
        filename = f"worker_{_safe_name(args.worker_name)}.log"
    path = os.path.join(log_dir, filename)

    if not os.path.isfile(path):
        print(f"tm-logs: stream file not found: {path}", file=sys.stderr)
        print(
            "tm-logs: no events recorded yet, or THOUGHTMACHINE_VAULT_ROOT "
            "points at a different vault",
            file=sys.stderr,
        )
        return 0

    try:
        records, skipped = _read_records(path)
    except OSError as exc:
        print(f"tm-logs: cannot read {path}: {exc}", file=sys.stderr)
        return 1

    if args.subcommand == "stop-reasons":
        rc = _run_stop_reasons(records, args)
    else:
        records = [rec for rec in records if _matches(rec, args)]
        _render_records(records, args, args.subcommand)
        rc = 0

    if skipped:
        print(
            f"tm-logs: skipped {skipped} malformed line(s) in {filename}",
            file=sys.stderr,
        )
    return rc


def main() -> int:
    """Entry point for the tm-logs CLI (also usable via ``python -m agent.cli.logs``)."""
    parser = build_parser()
    args = parser.parse_args()
    return _run(args, parser)


if __name__ == "__main__":
    sys.exit(main())
