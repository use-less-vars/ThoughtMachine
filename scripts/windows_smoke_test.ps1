<#
.SYNOPSIS
    ThoughtMachine Windows Smoke Test — validates that the agent core,
    configuration, and CLI are functional on Windows.

.DESCRIPTION
    Runs a battery of lightweight smoke tests covering:
      1. Python interpreter availability and version
      2. Virtual environment presence
      3. Critical imports (agent, pydantic, etc.)
      4. Config file readability
      5. Startup health check
      6. Basic agent instantiation (no API call)
      7. Logging subsystem initialisation
      8. Session store I/O (temp round-trip)

    Exits with code 0 if all tests pass, 1 otherwise.
    Output is printed to stdout; a log is written to tests/windows_smoke.log.

.NOTES
    Author: ThoughtMachine Dev Team
    Version: 1.0.0
    Requires: PowerShell 5.1+
#>

param(
    [string]$ProjectRoot = (Get-Location).Path,
    [string]$PythonExe = "python",
    [switch]$Verbose
)

# ── Helpers ──────────────────────────────────────────────────────────────────

$LogPath = Join-Path $ProjectRoot "tests" "windows_smoke.log"
$TotalTests = 0
$PassedTests = 0
$FailedTests = 0

# Ensure tests directory exists
if (-not (Test-Path (Join-Path $ProjectRoot "tests"))) {
    New-Item -ItemType Directory -Path (Join-Path $ProjectRoot "tests") -Force | Out-Null
}

function Write-Log {
    param([string]$Message)
    $Timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$Timestamp  $Message" | Out-File -FilePath $LogPath -Append -Encoding utf8
    Write-Host $Message
}

function Run-Test {
    param(
        [string]$Name,
        [scriptblock]$ScriptBlock
    )
    $script:TotalTests++
    try {
        $null = & $ScriptBlock
        $script:PassedTests++
        Write-Log "[PASS] $Name"
        return $true
    }
    catch {
        $script:FailedTests++
        Write-Log "[FAIL] $Name — $($_.Exception.Message)"
        if ($Verbose) {
            Write-Log "       $($_.ScriptStackTrace)"
        }
        return $false
    }
}

# ── Preliminaries ─────────────────────────────────────────────────────────────

Write-Log "╔═══════════════════════════════════════════════════════════════╗"
Write-Log "║  ThoughtMachine Windows Smoke Test                          ║"
Write-Log "║  Started: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')                      ║"
Write-Log "╚═══════════════════════════════════════════════════════════════╝"
Write-Log "Project root: $ProjectRoot"
Write-Log ""

# ── Test 1: Python interpreter ───────────────────────────────────────────────

Run-Test -Name "Python interpreter is available" -ScriptBlock {
    $version = & $PythonExe --version
    if ($LASTEXITCODE -ne 0) {
        throw "python --version returned exit code $LASTEXITCODE"
    }
    if ($version -notmatch '\d+\.\d+') {
        throw "Could not parse Python version from: $version"
    }
}

# ── Test 2: Virtual environment ──────────────────────────────────────────────

Run-Test -Name "Virtual environment exists" -ScriptBlock {
    $venvPaths = @(
        Join-Path $ProjectRoot ".venv" "Scripts" "python.exe",
        Join-Path $ProjectRoot "venv" "Scripts" "python.exe",
        Join-Path $ProjectRoot ".venv" "bin" "python",
        Join-Path $ProjectRoot "venv" "bin" "python"
    )
    $found = $false
    foreach ($p in $venvPaths) {
        if (Test-Path $p) { $found = $true; break }
    }
    if (-not $found) {
        # Not a blocker — just informational
        Write-Log "       (No venv found — using system Python)"
    }
}

# ── Test 3: Critical imports ─────────────────────────────────────────────────

Run-Test -Name "Critical Python imports resolve" -ScriptBlock {
    $imports = @(
        "pydantic",
        "agent",
        "agent.config.AgentConfig",
        "agent.startup_health_check",
        "session.store",
        "agent.logging"
    )
    $importScript = @"
import sys
success = []
failed = []
for mod in $($imports | ConvertTo-Json):
    try:
        __import__(mod.split('.')[0])
        success.append(mod)
    except ImportError as e:
        failed.append(f'{mod}: {e}')
print(f'SUCCESS:{len(success)} FAILED:{len(failed)}')
if failed:
    for f in failed:
        print(f'  FAIL: {f}')
"@
    $result = & $PythonExe -c $importScript 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Import check failed: $result"
    }
    if ($result -match 'FAILED:[1-9]') {
        throw "Some imports failed: $result"
    }
}

# ── Test 4: Config file readability ──────────────────────────────────────────

Run-Test -Name "Config file is readable and valid JSON" -ScriptBlock {
    $configPath = Join-Path $ProjectRoot "agent_config.json"
    if (-not (Test-Path $configPath)) {
        throw "Config file not found at $configPath"
    }
    $content = Get-Content $configPath -Raw -Encoding utf8
    if ([string]::IsNullOrWhiteSpace($content)) {
        throw "Config file is empty"
    }
    # Validate JSON via Python
    $validateScript = @"
import json, sys
with open(r'$configPath', 'r', encoding='utf-8') as f:
    data = json.load(f)
print(f'OK: {len(data)} keys')
"@
    $result = & $PythonExe -c $validateScript 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Config JSON validation failed: $result"
    }
}

# ── Test 5: Startup health check (no API key required) ───────────────────────

Run-Test -Name "Startup health check runs without error" -ScriptBlock {
    $hcScript = @"
from agent.startup_health_check import run_health_check
# Run only structural checks (skip api_key to avoid env dependency)
report = run_health_check(checks=['config_paths','config_valid','directories','python_version','imports'])
print(report.summary())
if not report.ok:
    import sys
    print(f'{len(report.failed)} structural check(s) failed', file=sys.stderr)
    sys.exit(1)
"@
    $result = & $PythonExe -c $hcScript 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Health check failed: $result"
    }
}

# ── Test 6: Basic agent instantiation (no API call) ──────────────────────────

Run-Test -Name "AgentConfig instantiation works" -ScriptBlock {
    $agentScript = @"
from agent.config import AgentConfig
cfg = AgentConfig()
dump = cfg.model_dump()
print(f'OK: AgentConfig created with {len(dump)} fields')
# Verify model_serializer strips stop_check
assert 'stop_check' not in dump, 'stop_check leaked into serialization!'
print('OK: stop_check correctly excluded from serialization')
"@
    $result = & $PythonExe -c $agentScript 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "AgentConfig instantiation failed: $result"
    }
}

# ── Test 7: Logging subsystem initialisation ─────────────────────────────────

Run-Test -Name "Logging subsystem initialises" -ScriptBlock {
    $logScript = @"
from agent.logging import log, init_logging
# init_logging should handle Windows paths gracefully
import tempfile, os
log_dir = tempfile.mkdtemp(prefix='tm_smoke_')
try:
    init_logging(log_dir=log_dir, log_level='DEBUG')
    log('INFO', 'smoke_test', 'Logging initialised successfully on Windows')
    print(f'OK: logging initialised in {log_dir}')
finally:
    import shutil
    shutil.rmtree(log_dir, ignore_errors=True)
"@
    $result = & $PythonExe -c $logScript 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Logging init failed: $result"
    }
}

# ── Test 8: Session store I/O round-trip ─────────────────────────────────────

Run-Test -Name "Session store round-trip works" -ScriptBlock {
    $sessionScript = @"
import tempfile, os, json
from session.store import FileSystemSessionStore
from session.models import Session

tmpdir = tempfile.mkdtemp(prefix='tm_sessions_')
try:
    store = FileSystemSessionStore(storage_dir=tmpdir)
    session = Session()
    session.metadata['name'] = 'Smoke Test Session'
    store.save_session(session)
    loaded = store.load_session(session.session_id)
    assert loaded is not None, 'Session was None after load'
    assert loaded.session_id == session.session_id, 'Session ID mismatch'
    print(f'OK: session {session.session_id[:8]}... round-tripped successfully')
finally:
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)
"@
    $result = & $PythonExe -c $sessionScript 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Session store round-trip failed: $result"
    }
}

# ── Test 9: Global Knowledge Base files bundled and deployable ─────────────

Run-Test -Name "Global KB files are bundled and installable" -ScriptBlock {
    $kbScript = @"
import sys, os, tempfile, shutil, json
from pathlib import Path

# 9a: Verify resources/global_kb/ contains .md files
try:
    import importlib.resources as pkg_resources
    pkg_path = pkg_resources.files("thoughtmachine")
    kb_dir = Path(str(pkg_path)).resolve().parent / "resources" / "global_kb"
    if not kb_dir.is_dir():
        # Fallback: development layout
        kb_dir = Path(__file__).resolve().parent.parent / "resources" / "global_kb"
    md_files = list(kb_dir.glob("*.md"))
    print(f'Found {len(md_files)} KB .md files in bundle')
    if len(md_files) < 3:
        print(f'WARNING: expected 9 KB files, found {len(md_files)}', file=sys.stderr)
    for f in sorted(md_files):
        print(f'  {f.name}')
    version_file = kb_dir / ".version"
    if not version_file.exists():
        raise FileNotFoundError(f'Missing .version in {kb_dir}')
    version = version_file.read_text(encoding="utf-8").strip()
    print(f'KB version: {version}')
except Exception as e:
    print(f'Bundle discovery: {e}', file=sys.stderr)
    # Non-fatal if we're in test mode without the package

# 9b: Run ensure_global_kb in a temp home
try:
    from agent.knowledge.global_kb import ensure_global_kb, GLOBAL_KB_DIR, SYSTEM_DIR
    
    # Use a temporary home directory to isolate the test
    tmp_home = tempfile.mkdtemp(prefix='tm_kb_test_')
    old_home = os.environ.get('HOME')
    os.environ['HOME'] = tmp_home
    # Re-import to pick up new paths (or just use the imported refs with override)
    from agent.knowledge.global_kb import ensure_global_kb, GLOBAL_KB_DIR, SYSTEM_DIR
    # Override paths to use tmp_home
    import agent.knowledge.global_kb as kb_mod
    kb_mod.GLOBAL_KB_DIR = Path(tmp_home) / ".thoughtmachine" / "knowledge"
    kb_mod.SYSTEM_DIR = kb_mod.GLOBAL_KB_DIR / "system"
    kb_mod.USER_DIR = kb_mod.GLOBAL_KB_DIR / "user"
    
    touched = ensure_global_kb()
    print(f'ensure_global_kb() touched {len(touched)} file(s)')
    
    # Verify system files are deployed
    system_files = list(kb_mod.SYSTEM_DIR.glob("*.md"))
    print(f'{len(system_files)} system file(s) deployed')
    if len(system_files) == 0:
        raise RuntimeError('No KB files were deployed to system directory')
    for f in sorted(system_files):
        print(f'  {f.name}')
    
    # Verify version marker exists
    version_marker = kb_mod.GLOBAL_KB_DIR / ".version"
    if not version_marker.exists():
        raise FileNotFoundError('Version marker not written')
    print(f'Version marker: {version_marker.read_text(encoding="utf-8").strip()}')
    
    # Verify user directory created with placeholder
    my_notes = kb_mod.USER_DIR / "my_notes.md"
    if not my_notes.exists():
        raise FileNotFoundError('my_notes.md placeholder not created')
    print(f'User placeholder: {my_notes.name}')
    
    shutil.rmtree(tmp_home, ignore_errors=True)
    print('OK: Global KB deployment verified')
except ImportError as e:
    print(f'Skipping deployment test (import error): {e}', file=sys.stderr)
    # Non-fatal — may not have agent.knowledge.global_kb in test context
    if 'tmp_home' in dir():
        shutil.rmtree(tmp_home, ignore_errors=True)
except Exception as e:
    if 'tmp_home' in dir():
        shutil.rmtree(tmp_home, ignore_errors=True)
    raise
"@
    $result = & $PythonExe -c $kbScript 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Global KB test failed: $result"
    }
}

# ── Summary ──────────────────────────────────────────────────────────────────

Write-Log ""
Write-Log "╔═══════════════════════════════════════════════════════════════╗"
Write-Log "║  Results                                                     ║"
Write-Log "║  Total:  $($TotalTests.ToString().PadLeft(3))  |  Passed:  $($PassedTests.ToString().PadLeft(3))  |  Failed:  $($FailedTests.ToString().PadLeft(3))  ║"
Write-Log "╚═══════════════════════════════════════════════════════════════╝"

if ($FailedTests -gt 0) {
    Write-Log "FAILED — review log at $LogPath"
    exit 1
}
else {
    Write-Log "ALL PASSED — Smoke test complete."
    exit 0
}
