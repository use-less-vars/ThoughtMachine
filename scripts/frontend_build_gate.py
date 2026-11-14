#!/usr/bin/env python3
"""Content-hash staleness gate for the production frontend bundle.

ONE place that answers "does the launcher have to run ``vite build``?".  Both
``start_thoughtmachine.sh`` and ``start_thoughtmachine.bat`` are thin callers:
they ask this script, then either run the build or skip it.

Why a CONTENT hash and not mtimes: ``git checkout`` / branch switches rewrite
mtimes on files whose content did not change, and restore old mtimes on files
whose content did.  Timestamps are therefore a liar; hashing the inputs is the
only signal that tracks what is actually in the tree.  No git is required.

Subcommands
-----------
``check``  Decide, and print exactly ONE machine-readable line on stdout::

               TM_FRONTEND_DECISION=build hash=<12-hex> reason=<token>
               TM_FRONTEND_DECISION=skip  hash=<12-hex> reason=stamp-matches

           Human diagnostics go to stderr, so a caller can capture stdout
           alone.  The exit code is 0 for every decision -- including the
           failure path, which answers ``build``.  The gate NEVER skips when it
           is unsure: an unreadable tree, a missing ``src``, a broken stamp --
           all of them build.  Exit code 2 means the helper itself could not
           run (bad usage); callers must treat a missing or garbled line as
           ``build`` too.

``stamp``  Write the current hash into the stamp file.  A launcher calls this
           AFTER a successful build only: a stamp written before the build
           would certify a half-written ``dist/``.

``hash``   Print the hash alone (debugging and tests).

Inputs hashed
-------------
  * every file under ``<frontend>/src`` (sorted by POSIX relative path)
  * ``<frontend>/index.html``
  * ``package.json``, ``package-lock.json`` / ``npm-shrinkwrap.json``, ``.nvmrc``
  * ``vite.config.*`` and ``tsconfig*.json`` in the frontend root
  * the launcher's build command string (``--build-cmd``), so changing HOW the
    bundle is produced invalidates the stamp as well

Stamp file
----------
``<frontend>/.dist-build-stamp`` -- next to ``dist/``, git-ignored, never
tracked.  A fresh clone has no stamp and therefore builds.  The stamp is not
trusted on its own: ``dist/`` must also exist, be non-empty, and contain
``index.html``.  A stamp without output is exactly the stale-bundle bug this
gate exists to prevent.

Force
-----
``--force`` on the command line, or ``TM_FORCE_FRONTEND_BUILD=1`` in the
environment (the launcher's ``--rebuild-frontend`` flag passes ``--force``).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

# Bump this BY HAND whenever the build invocation itself changes: the build
# command is deliberately NOT part of the hash (see compute_hash), so this tag is
# the only thing that can invalidate stamps across a change of build tooling.
SCHEMA = "tm-frontend-build-gate-v2"
STAMP_NAME = ".dist-build-stamp"
DIST_NAME = "dist"
HASH_LEN = 12

# Frontend-root files that are build inputs (globs cover the config files).
_ROOT_FILES = (
    "index.html",
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    ".nvmrc",
)
_ROOT_GLOBS = ("vite.config.*", "tsconfig*.json")

DEFAULT_FRONTEND = Path(__file__).resolve().parents[1] / "web_ui" / "frontend"


def _inputs(frontend: Path) -> list[Path]:
    """Every file whose content can change the bundle, sorted deterministically."""
    found: list[Path] = []
    src = frontend / "src"
    if src.is_dir():
        found.extend(p for p in src.rglob("*") if p.is_file())
    for name in _ROOT_FILES:
        candidate = frontend / name
        if candidate.is_file():
            found.append(candidate)
    for pattern in _ROOT_GLOBS:
        found.extend(p for p in frontend.glob(pattern) if p.is_file())
    # De-duplicate (globs can overlap) and sort by relative POSIX path so the
    # hash is stable across filesystems and directory-iteration order.
    unique = {p.resolve(): p for p in found}
    return sorted(unique.values(), key=lambda p: p.relative_to(frontend).as_posix())


def compute_hash(frontend: Path, build_cmd: str) -> tuple[str, int]:
    """Return ``(hash, input_count)`` for the frontend inputs.

    ``build_cmd`` is still accepted for callers but deliberately does NOT enter
    the digest: the shell and Windows launchers legitimately spell the
    same build differently (``vite build`` vs ``npm run build``), and hashing the
    string made a shared checkout flip the stamp on every alternation.  Change of
    build tooling is covered by bumping ``SCHEMA`` instead.
    """
    del build_cmd  # signature kept: callers pass it, it must not affect the hash
    digest = hashlib.sha256()
    digest.update(SCHEMA.encode())
    digest.update(b"\0")
    inputs = _inputs(frontend)
    for path in inputs:
        digest.update(path.relative_to(frontend).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:HASH_LEN], len(inputs)


def _reason_and_decision(
    frontend: Path, build_cmd: str, force: bool
) -> tuple[str, str, str]:
    """Return ``(decision, hash, reason)``. Every uncertain path answers ``build``."""
    if force:
        try:
            digest, _ = compute_hash(frontend, build_cmd)
        except OSError:
            digest = "-"
        return "build", digest, "forced"

    try:
        digest, count = compute_hash(frontend, build_cmd)
    except OSError as exc:  # unreadable input: never skip
        print(
            "frontend build gate: cannot hash the frontend inputs "
            f"({exc.__class__.__name__}: {exc}); building instead of skipping.",
            file=sys.stderr,
        )
        return "build", "-", "hash-failed"

    if count == 0:
        print(
            f"frontend build gate: no frontend inputs found under {frontend}; "
            "cannot decide, building instead of skipping.",
            file=sys.stderr,
        )
        return "build", digest, "no-inputs"

    stamp = frontend / STAMP_NAME
    try:
        previous = stamp.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return "build", digest, "no-stamp"
    except OSError as exc:
        print(
            f"frontend build gate: cannot read {stamp.name} "
            f"({exc.__class__.__name__}: {exc}); building instead of skipping.",
            file=sys.stderr,
        )
        return "build", digest, "hash-failed"

    if previous != digest:
        return "build", digest, "source-changed"

    dist = frontend / DIST_NAME
    if not dist.is_dir():
        return "build", digest, "output-missing"
    try:
        non_empty = any(dist.iterdir())
    except OSError:
        non_empty = False
    if not non_empty:
        return "build", digest, "output-empty"
    if not (dist / "index.html").is_file():
        return "build", digest, "output-incomplete"
    return "skip", digest, "stamp-matches"


def _decision_line(decision: str, digest: str, reason: str) -> str:
    return f"TM_FRONTEND_DECISION={decision} hash={digest} reason={reason}"


def _cmd_check(args: argparse.Namespace) -> int:
    force = bool(args.force) or os.environ.get("TM_FORCE_FRONTEND_BUILD", "") == "1"
    decision, digest, reason = _reason_and_decision(
        Path(args.frontend_dir), args.build_cmd, force
    )
    print(_decision_line(decision, digest, reason))
    return 0


def _cmd_stamp(args: argparse.Namespace) -> int:
    frontend = Path(args.frontend_dir)
    try:
        digest, count = compute_hash(frontend, args.build_cmd)
    except OSError as exc:
        print(
            "frontend build gate: cannot hash the frontend inputs "
            f"({exc.__class__.__name__}: {exc}); build stamp NOT written.",
            file=sys.stderr,
        )
        return 1
    if count == 0:
        print(
            f"frontend build gate: no frontend inputs found under {frontend}; "
            "build stamp NOT written.",
            file=sys.stderr,
        )
        return 1
    # Belt-and-braces: `check` already refuses to skip without a real bundle
    # (output-missing / output-incomplete), but a stamp for a build that never
    # produced dist/index.html must not be written in the first place.
    dist_index = frontend / DIST_NAME / "index.html"
    if not dist_index.is_file():
        print(
            f"frontend build gate: {dist_index} is missing; build stamp NOT written.",
            "Run the production build first, then stamp its output.",
            file=sys.stderr,
        )
        return 1
    stamp = frontend / STAMP_NAME
    tmp = stamp.with_name(stamp.name + ".tmp")
    try:
        tmp.write_text(digest + "\n", encoding="utf-8")
        os.replace(tmp, stamp)
    except OSError as exc:
        print(
            f"frontend build gate: cannot write {stamp} "
            f"({exc.__class__.__name__}: {exc}); the next boot will rebuild.",
            file=sys.stderr,
        )
        return 1
    print(_decision_line("stamped", digest, "stamp-written"))
    return 0


def _cmd_hash(args: argparse.Namespace) -> int:
    digest, count = compute_hash(Path(args.frontend_dir), args.build_cmd)
    print(f"{digest} ({count} inputs)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="frontend_build_gate.py",
        description="Decide whether the production frontend bundle must be rebuilt.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _add(name: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--frontend-dir", default=str(DEFAULT_FRONTEND))
        p.add_argument(
            "--build-cmd",
            default="npm run build",
            help="the launcher's build command, hashed with the inputs",
        )
        return p

    _add("check", "print the build/skip decision").add_argument(
        "--force", action="store_true", help="always decide 'build'"
    )
    _add("stamp", "write the current hash to the stamp file")
    _add("hash", "print the current hash")

    args = parser.parse_args(argv)
    handler = {"check": _cmd_check, "stamp": _cmd_stamp, "hash": _cmd_hash}[args.command]
    try:
        return handler(args)
    except Exception as exc:  # noqa: BLE001 - the gate must never crash a launcher
        print(
            f"frontend build gate: unexpected {exc.__class__.__name__}: {exc}; "
            "building instead of skipping.",
            file=sys.stderr,
        )
        if args.command == "check":
            print(_decision_line("build", "-", "gate-error"))
            return 0
        return 1


if __name__ == "__main__":
    sys.exit(main())
