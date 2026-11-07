"""
Entry point for PyInstaller-built ThoughtMachine executable.

This wrapper:
1. Auto-enables --serve-frontend (the frontend is bundled)
2. Sets up sys.path so all packages are found inside the PyInstaller bundle
3. Works cross-platform (Windows, macOS, Linux)
"""
import sys
import os

# ---- Stdio guard -------------------------------------------------------
# If this process is launched with file descriptors 1/2 closed (e.g. by a
# headless launcher or service wrapper), CPython sets sys.stdout / sys.stderr
# to None.  Any print(), warnings emission, or stdlib-logging lastResort
# fallback then crashes with "'NoneType' object has no attribute 'write'"
# (this is the DockerCodeRunner crash).  Redirect them to devnull so the
# whole process is stdio-safe in any launch environment.
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')


def main() -> None:
    """Launch ThoughtMachine web UI server with frontend serving enabled."""

    # ── Adjust sys.path for PyInstaller one-folder builds ────────────────
    # In a PyInstaller --onedir build, the entry-point script lives next to
    # the _internal/ directory.  The _internal/ directory contains all
    # packages.  If it is present, add it so that `import agent` etc. work.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass is not None:
        # one-file build -- _MEIPASS points to the temp extraction dir
        if meipass not in sys.path:
            sys.path.insert(0, meipass)
    else:
        # one-folder build -- packages live in _internal/
        bundle_dir = os.path.dirname(os.path.abspath(__file__))
        internal = os.path.join(bundle_dir, "_internal")
        if os.path.isdir(internal) and internal not in sys.path:
            sys.path.insert(0, internal)

    # Ensure the project root (bundle dir) itself is on sys.path too
    bundle_dir = os.path.dirname(os.path.abspath(__file__))
    if bundle_dir not in sys.path:
        sys.path.insert(0, bundle_dir)

    # ── Add --serve-frontend by default ──────────────────────────────────
    if "--serve-frontend" not in sys.argv and "--no-frontend" not in sys.argv:
        # Insert right after the script name so argparse still sees it
        sys.argv.insert(1, "--serve-frontend")

    # ── Launch the server ────────────────────────────────────────────────
    from web_ui.backend.server import main as server_main
    server_main()


if __name__ == "__main__":
    main()
