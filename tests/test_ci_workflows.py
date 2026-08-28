"""CI workflow structure tests.

Reads the GitHub Actions workflow files under .github/workflows/ and asserts
the cross-platform smoke matrix exists and targets the real backend health
contract: GET /api/health (the legacy /health route does not exist in the
backend; scripts/smoke_windows.ps1 was updated to poll /api/health).

Parsing is dependency-light: PyYAML is used when importable (it is part of
requirements.txt), otherwise plain-string assertions fall back.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
SMOKE_WORKFLOW = WORKFLOWS_DIR / "cross-platform-smoke.yml"
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "smoke_windows.ps1"

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
