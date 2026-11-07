"""Self-tests for the hermetic pytest environment (feat/hermetic-vault-tests).

These tests verify that the pytest session never writes to the REAL user vault
(``~/.thoughtmachine`` on the host/container home):

* ``THOUGHTMACHINE_VAULT_ROOT`` is absent or points away from the real vault,
* ``Path.home()`` / ``os.path.expanduser("~")`` resolve outside the real home,
* production modules that bind home-derived paths at import time point at the
  redirected temp vault,
* the session filesystem guard raises ``RuntimeError`` (``HERMETIC VAULT
  GUARD``) on every write targeting the real vault,
* writes to the redirected vault still succeed.
"""

import os
import pathlib
import shutil

import pytest

GUARD_DISABLED = os.environ.get("THOUGHTMACHINE_HERMETIC_GUARD_DISABLED") == "1"

pytestmark = pytest.mark.skipif(
    GUARD_DISABLED, reason="THOUGHTMACHINE_HERMETIC_GUARD_DISABLED=1"
)


# ---------------------------------------------------------------------------
# Environment is redirected away from the real home / vault.
# ---------------------------------------------------------------------------


def test_vault_root_env_not_pointing_at_real_vault(real_vault_paths):
    _, real_vault = real_vault_paths
    env_root = os.environ.get("THOUGHTMACHINE_VAULT_ROOT")
    assert env_root is None or os.path.realpath(env_root) != real_vault


def test_path_home_points_away_from_real_vault(real_vault_paths):
    real_home, real_vault = real_vault_paths
    home = os.path.realpath(pathlib.Path.home())
    assert home != real_home
    assert os.path.realpath(pathlib.Path.home() / ".thoughtmachine") != real_vault


def test_expanduser_points_away_from_real_vault(real_vault_paths):
    real_home, real_vault = real_vault_paths
    home = os.path.realpath(os.path.expanduser("~"))
    assert home != real_home
    assert os.path.realpath(os.path.expanduser("~/.thoughtmachine")) != real_vault


def test_production_home_bindings_point_at_temp_vault(real_vault_paths):
    _, real_vault = real_vault_paths
    bindings = (
        ("thoughtmachine.bootstrap", "USER_DIR"),
        ("thoughtmachine.security", "VAULT_ROOT"),
        ("agent.knowledge.global_kb", "GLOBAL_KB_DIR"),
        ("agent.knowledge.global_kb", "USER_DIR"),
        ("agent.config.loader", "USER_DIR"),
        ("agent.config.provider_profile", "THOUGHTMACHINE_DIR"),
        ("tools.mcp_server_connect", "REGISTRY_PATH"),
    )
    for module_name, attr in bindings:
        try:
            mod = __import__(module_name, fromlist=[attr])
            value = getattr(mod, attr)
        except Exception:
            # Optional-dependency module not importable in this env; the
            # fs guard still backstops it.
            continue
        assert os.path.realpath(value) != real_vault


# ---------------------------------------------------------------------------
# The fs guard blocks every write into the real vault.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op",
    [
        "makedirs", "mkdir", "rmdir", "remove", "unlink",
        "rename", "replace", "rmtree", "move",
        "open_write", "os_open", "utime", "chmod",
    ],
)
def test_guard_blocks_real_vault_writes(real_vault_paths, tmp_path, op):
    _, real_vault = real_vault_paths
    target = os.path.join(real_vault, "guard-test", "file.txt")
    dst = os.path.join(str(tmp_path), "dst.txt")
    with pytest.raises(RuntimeError, match="HERMETIC VAULT GUARD"):
        if op in ("makedirs", "mkdir", "rmdir", "remove", "unlink"):
            getattr(os, op)(target)
        elif op in ("rename", "replace"):
            getattr(os, op)(target, dst)
        elif op == "rmtree":
            shutil.rmtree(target)
        elif op == "move":
            shutil.move(target, dst)
        elif op == "open_write":
            open(target, "w")
        elif op == "os_open":
            os.open(target, os.O_WRONLY | os.O_CREAT)
        elif op == "utime":
            os.utime(target)
        elif op == "chmod":
            os.chmod(target, 0o644)


def test_guard_blocks_mkdir_via_pathlib(real_vault_paths):
    _, real_vault = real_vault_paths
    with pytest.raises(RuntimeError, match="HERMETIC VAULT GUARD"):
        (pathlib.Path(real_vault) / "x").mkdir()


def test_guard_allows_reads_of_real_vault(real_vault_paths):
    _, real_vault = real_vault_paths
    with pytest.raises(FileNotFoundError):
        open(os.path.join(real_vault, "definitely-not-here"), "r")


# ---------------------------------------------------------------------------
# Writes to the redirected vault still work.
# ---------------------------------------------------------------------------


def test_writes_to_redirected_vault_succeed(tmp_path):
    vault = tmp_path / ".thoughtmachine"
    (vault / "credentials").mkdir(parents=True)
    cred_file = vault / "credentials" / "test_key"
    cred_file.write_text("secret")
    assert cred_file.read_text() == "secret"
    os.remove(str(cred_file))
    assert not cred_file.exists()
