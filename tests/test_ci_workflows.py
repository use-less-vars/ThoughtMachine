"""CI workflow structure tests.

Reads the GitHub Actions workflow files under .github/workflows/ and asserts
the cross-platform smoke matrix exists and targets the real backend health
contract: GET /api/health (the legacy /health route does not exist in the
backend; scripts/smoke_windows.ps1 was updated to poll /api/health).

Parsing is dependency-light: PyYAML is used when importable (it is part of
requirements.txt), otherwise plain-string assertions fall back.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
SMOKE_WORKFLOW = WORKFLOWS_DIR / "cross-platform-smoke.yml"
FIRST_START_WORKFLOW = WORKFLOWS_DIR / "first_start_ci.yml"
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "smoke_windows.ps1"
ASSERT_SCRIPT = REPO_ROOT / "scripts" / "assert_check_only_not_vacuous.sh"

try:
    import yaml
except ImportError:  # pragma: no cover - depends on the test environment
    yaml = None


def _workflow():
    """Return (parsed yaml dict or None, raw workflow text)."""
    assert SMOKE_WORKFLOW.is_file(), (
        f"missing workflow: {SMOKE_WORKFLOW.relative_to(REPO_ROOT)}"
    )
    text = SMOKE_WORKFLOW.read_text(encoding="utf-8")
    if yaml is None:
        return None, text
    return yaml.safe_load(text), text


def _job_runs(data, text, job):
    """Joined `run` scripts of one job (yaml path) or the raw text (fallback)."""
    if data is not None:
        steps = data["jobs"][job]["steps"]
        return "\n".join(
            s.get("run", "") or "" for s in steps if isinstance(s, dict)
        )
    return text


_JOB_KEY = re.compile(r"^  [A-Za-z0-9_-]+:\s*$")


def _job_text(text, job):
    """Raw text block of one top-level job (its key up to the next job key)."""
    lines = text.splitlines()
    start = next(
        (i for i, ln in enumerate(lines) if ln.rstrip() == f"  {job}:"), None
    )
    assert start is not None, f"job {job!r} not found in workflow text"
    end = next(
        (j for j in range(start + 1, len(lines)) if _JOB_KEY.match(lines[j])),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_smoke_workflow_exists():
    assert SMOKE_WORKFLOW.is_file()


def test_workflow_contains_both_jobs():
    data, text = _workflow()
    if data is not None:
        jobs = data["jobs"]
        assert "linux-install" in jobs
        assert "windows-smoke" in jobs
    else:
        assert "linux-install:" in text
        assert "windows-smoke:" in text


def test_workflow_yaml_parses():
    if yaml is None:
        pytest.skip("PyYAML not available in this environment")
    data, _ = _workflow()
    assert isinstance(data, dict)
    assert isinstance(data.get("jobs"), dict)
    assert len(data["jobs"]) >= 2


def test_linux_job_installs_and_smokes_api_health():
    data, text = _workflow()
    runs = _job_runs(data, text, "linux-install")
    assert "bash install.sh" in runs
    assert "--check-only" in runs
    assert "/api/health" in runs


def test_windows_job_references_api_health():
    data, text = _workflow()
    runs = _job_runs(data, text, "windows-smoke")
    assert "smoke_windows.ps1" in runs
    assert "/api/health" in runs


def test_smoke_windows_script_polls_api_health():
    assert SMOKE_SCRIPT.is_file()
    text = SMOKE_SCRIPT.read_text(encoding="utf-8")
    assert "http://127.0.0.1:8000/api/health" in text
    # No legacy /health route polling may remain (the backend has no such route).
    without_api = text.replace("http://127.0.0.1:8000/api/health", "")
    assert "127.0.0.1:8000/health" not in without_api


# --- non-vacuity assertion script (Part A) ----------------------------------

# Verbatim anchors from start_thoughtmachine.sh at this commit:
#   :238 header, :307 usable outcome, :247/:269/:291 the --check-only WARNINGs.
_HEADER = "[2/8] Docker ..."
_OK = "      ok."  # start_thoughtmachine.sh:307 (exactly six leading spaces)
_WARNING = (
    "      WARNING: Docker is not usable (reason: lib_missing) - "
    "continuing in --check-only mode."
)  # start_thoughtmachine.sh:269


def _make_log(section_body, with_header=True):
    """A launcher-shaped log whose [2/8] section body is *section_body*."""
    lines = ["[1/8] Python environment", _OK]
    if with_header:
        lines.append(_HEADER)
        lines.extend(section_body)
    lines += ["[3/8] Ports", _OK]
    return "\n".join(lines) + "\n"


def _run_assert(tmp_path, log_text, *extra):
    log = tmp_path / "check-only.log"
    log.write_text(log_text, encoding="utf-8")
    return subprocess.run(
        ["bash", str(ASSERT_SCRIPT), *extra, str(log)],
        capture_output=True,
        text=True,
    )


def test_assert_script_exists():
    assert ASSERT_SCRIPT.is_file()


def test_assert_default_rejects_vacuous_warning_log(tmp_path):
    # A Docker WARNING with no usable outcome proved nothing about Docker.
    res = _run_assert(tmp_path, _make_log([_WARNING]))
    assert res.returncode != 0, res.stdout + res.stderr
    assert "FAIL" in res.stderr


def test_assert_default_accepts_healthy_log(tmp_path):
    res = _run_assert(tmp_path, _make_log([_OK]))
    assert res.returncode == 0, res.stdout + res.stderr


def test_assert_default_rejects_missing_section(tmp_path):
    res = _run_assert(tmp_path, _make_log([], with_header=False))
    assert res.returncode != 0, res.stdout + res.stderr


def test_assert_expect_no_docker_accepts_named_warning(tmp_path):
    # Docker-less lane BY CONTRACT: a named-reason Docker WARNING with no
    # usable outcome is exactly the expected shape, so this PASSES.
    res = _run_assert(tmp_path, _make_log([_WARNING]), "--expect-no-docker")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "PASS" in res.stdout


def test_assert_expect_docker_rejects_named_warning(tmp_path):
    # Same log under the (default) Docker-expected contract must FAIL.
    res = _run_assert(tmp_path, _make_log([_WARNING]), "--expect-docker")
    assert res.returncode != 0, res.stdout + res.stderr
    assert "FAIL" in res.stderr


def test_assert_expect_no_docker_rejects_usable_outcome(tmp_path):
    # A usable outcome means the lane's docker-less contract is STALE.
    res = _run_assert(tmp_path, _make_log([_OK]), "--expect-no-docker")
    assert res.returncode != 0, res.stdout + res.stderr
    assert "FAIL" in res.stderr


def test_assert_expect_no_docker_rejects_unnamed_warning(tmp_path):
    # A Docker WARNING that names NO specific reason proves nothing -> FAIL.
    unnamed = "      WARNING: Docker is not usable"
    res = _run_assert(tmp_path, _make_log([unnamed]), "--expect-no-docker")
    assert res.returncode != 0, res.stdout + res.stderr
    assert "FAIL" in res.stderr


def test_assert_rejects_ok_plus_warning(tmp_path):
    # Single-mode regression: a section carrying BOTH "      ok." and a Docker
    # WARNING still fails (the retired Docker-absent-tolerant mode let this
    # through: its OR predicate accepted ok=1 despite the WARNING).
    res = _run_assert(tmp_path, _make_log([_OK, _WARNING]))
    assert res.returncode != 0, res.stdout + res.stderr
    assert "FAIL" in res.stderr


def test_assert_missing_logfile_is_usage_error(tmp_path):
    res = subprocess.run(
        ["bash", str(ASSERT_SCRIPT), str(tmp_path / "nope.log")],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 2, res.stdout + res.stderr


def test_linux_job_asserts_non_vacuous_check_only():
    data, text = _workflow()
    runs = _job_runs(data, text, "linux-install")
    assert "assert_check_only_not_vacuous.sh" in runs
    assert "pipefail" in runs


def test_first_start_workflow_asserts_non_vacuous_check_only():
    assert FIRST_START_WORKFLOW.is_file()
    text = FIRST_START_WORKFLOW.read_text(encoding="utf-8")
    assert "assert_check_only_not_vacuous.sh" in text
    assert "pipefail" in text
    assert "--expect-no-docker" in text


def test_macos_launcher_job_is_gating_on_macos():
    data, text = _workflow()
    # Plain-text fallback (runs even if PyYAML is absent): the macos-launcher
    # job block must stay GATING - it carries no continue-on-error.
    block = _job_text(text, "macos-launcher")
    assert block.strip(), "empty macos-launcher block"
    assert "continue-on-error" not in block
    if data is None:
        pytest.skip("PyYAML not available in this environment")
    job = data["jobs"]["macos-launcher"]
    assert job["runs-on"] == "macos-latest"
    assert "continue-on-error" not in job

