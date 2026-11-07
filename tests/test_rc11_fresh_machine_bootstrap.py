"""RC11: a fresh machine must be able to start.

Regression tests for the fresh-machine bootstrap entry point.  They exercise
the real CLI surface in **subprocesses** (so the in-process hermetic guard in
``tests/conftest.py`` — which patches ``os``/``open``/``chmod`` and redirects
``HOME`` — does not apply), each with a throwaway ``HOME`` so nothing ever
touches the real ``~/.thoughtmachine``.

Covered:

* ``python -m thoughtmachine.bootstrap`` creates ``$HOME/.thoughtmachine`` with
  owner-only (``0o700``) permissions, and is idempotent.
* the read-only ``scripts/doctor_checks.py --check-dotthoughtmachine`` check
  fails on a fresh ``HOME`` and passes after bootstrap ran.
* the launchers run bootstrap *before* the doctor check (static order check).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
START_SH = REPO_ROOT / "start_thoughtmachine.sh"
INSTALL_SH = REPO_ROOT / "install_thoughtmachine.sh"
DOCTOR_CHECKS = REPO_ROOT / "scripts" / "doctor_checks.py"


def _clean_env(home: Path) -> dict:
    """Return an environment copy with a throwaway HOME and repo on PYTHONPATH.

    Any ``*VAULT_ROOT*`` variable (notably ``THOUGHTMACHINE_VAULT_ROOT``) is
    removed so ``thoughtmachine.vault.vault_root()`` derives the vault from
    HOME rather than the ambient environment.
    """
    env = dict(os.environ)
    env["HOME"] = str(home)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + existing if existing else "")
    for key in list(env):
        if "VAULT_ROOT" in key.upper():
            del env[key]
    return env


def _run(args, env):
    return subprocess.run(
        args,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def _bootstrap(home: Path):
    env = _clean_env(home)
    return env, _run([sys.executable, "-m", "thoughtmachine.bootstrap"], env)


def test_bootstrap_module_creates_vault_0700(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env, proc = _bootstrap(home)

    assert proc.returncode == 0, proc.stdout + proc.stderr

    vault = home / ".thoughtmachine"
    assert vault.is_dir()

    mode = stat.S_IMODE(vault.stat().st_mode)
    assert mode == 0o700, f"expected 0o700, got 0o{mode:03o}"

    # Idempotent: a second run succeeds and does not change the permissions.
    proc2 = _run([sys.executable, "-m", "thoughtmachine.bootstrap"], env)
    assert proc2.returncode == 0, proc2.stdout + proc2.stderr
    assert stat.S_IMODE(vault.stat().st_mode) == 0o700


def test_doctor_check_passes_after_bootstrap(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = _clean_env(home)

    # Before bootstrap: the vault is absent, so the read-only check must fail.
    before = _run([sys.executable, str(DOCTOR_CHECKS), "--check-dotthoughtmachine"], env)
    assert before.returncode != 0, before.stdout + before.stderr
    before_json = json.loads(before.stdout)
    assert before_json.get("ok") is False

    # Run bootstrap, then the very same read-only check must pass.
    proc = _run([sys.executable, "-m", "thoughtmachine.bootstrap"], env)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    after = _run([sys.executable, str(DOCTOR_CHECKS), "--check-dotthoughtmachine"], env)
    assert after.returncode == 0, after.stdout + after.stderr
    after_json = json.loads(after.stdout)
    assert after_json.get("ok") is True


@pytest.mark.skipif(
    not START_SH.exists() or not INSTALL_SH.exists(),
    reason="launcher scripts not present in this checkout",
)
def test_launcher_orders_bootstrap_before_check():
    start_text = START_SH.read_text(encoding="utf-8")

    bootstrap_at = start_text.find("thoughtmachine.bootstrap")
    check_at = start_text.find("--check-dotthoughtmachine")

    assert bootstrap_at != -1, "start launcher does not invoke thoughtmachine.bootstrap"
    assert check_at != -1, "start launcher does not run --check-dotthoughtmachine"
    assert bootstrap_at < check_at, (
        "bootstrap must run BEFORE the read-only doctor check in start_thoughtmachine.sh"
    )

    # The failure-path guidance stays intact.
    assert "chown -R" in start_text
    assert "Vault not writable" in start_text

    # The install script initialises the vault during install too.
    assert "thoughtmachine.bootstrap" in INSTALL_SH.read_text(encoding="utf-8")
