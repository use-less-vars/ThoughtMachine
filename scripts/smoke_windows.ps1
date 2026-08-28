# smoke_windows.ps1 - Windows smoke test for ThoughtMachine.
#
# Checks prerequisites, creates/verifies the venv, installs Python
# dependencies, verifies imports, starts the backend and polls its
# health endpoint.
#
# Usage:
#     powershell -ExecutionPolicy Bypass -File scripts\smoke_windows.ps1
#
# Exit codes:
#     0  all checks passed
#     1  one or more checks failed
#
# This file is intentionally pure ASCII (no non-ASCII characters).

$ErrorActionPreference = "Stop"

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::ASCII
} catch {
    # No console (e.g. redirected output) - keep default encoding.
}

$script:failures = 0

$Root = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $Root ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$Log = Join-Path $Root "smoke_windows.log"

function Write-Step($msg) {
    Write-Host "==> $msg"
}

function Write-Fail($msg) {
    Write-Host "FAIL: $msg" -ForegroundColor Red
    $script:failures++
}

function Find-Python {
    if (Test-Path $VenvPython) {
        return $VenvPython
    }
    foreach ($name in @("py", "python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) {
            return $cmd
        }
    }
    return $null
}

function Get-VersionMajor($text) {
    $m = [regex]::Match($text, "(\d+)")
    if ($m.Success) {
        return [int]$m.Groups[1].Value
    }
    return 0
}

Write-Step "Smoke test started ($(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))"

# 1. Python
$py = Find-Python
if ($null -eq $py) {
    Write-Fail "Python not found. Install Python 3.11+ and rerun."
} else {
    $pyOut = (& $py --version 2>&1 | Out-String).Trim()
    if ((Get-VersionMajor $pyOut) -lt 3) {
        Write-Fail "Python too old: $pyOut"
    } else {
        Write-Host "OK: Python: $pyOut"
    }
}

# 2. Node.js
$node = Get-Command node -ErrorAction SilentlyContinue
if ($null -eq $node) {
    Write-Fail "Node.js not found. Install Node.js 18+ and rerun."
} else {
    $nodeOut = (& $node --version 2>&1 | Out-String).Trim()
    if ((Get-VersionMajor $nodeOut) -lt 18) {
        Write-Fail "Node.js too old: $nodeOut (need 18+)"
    } else {
        Write-Host "OK: Node.js: $nodeOut"
    }
}

# 3. Docker Desktop (warning only)
try {
    docker info *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "OK: Docker Desktop is running"
    } else {
        Write-Host "Docker Desktop is required for full functionality. Some features will be disabled."
    }
} catch {
    Write-Host "Docker Desktop is required for full functionality. Some features will be disabled."
}

# 4. venv
Write-Step "Checking virtual environment"
if (-not (Test-Path $VenvPython)) {
    if ($null -eq $py) {
        Write-Fail "Cannot create venv: Python not found."
        exit 1
    }
    & $py -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Failed to create venv at $VenvDir"
        exit 1
    }
    Write-Host "OK: venv created at $VenvDir"
} else {
    Write-Host "OK: venv already exists"
}
if (-not (Test-Path $VenvPython)) {
    Write-Fail "venv python not found at $VenvPython"
    exit 1
}

# 5. Install Python dependencies
Write-Step "Installing Python dependencies"
& $VenvPython -m pip install --upgrade pip | Out-Null
& $VenvPython -m pip install -r (Join-Path $Root "requirements.txt") *> $Log
if ($LASTEXITCODE -ne 0) {
    Write-Fail "pip install failed - see $Log"
    exit 1
}
Write-Host "OK: dependencies installed"

# 6. Imports
Write-Step "Verifying critical imports"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
Push-Location $Root
try {
    $importCode = "import fastapi, uvicorn, pydantic, web_ui.backend.server; print('IMPORTS_OK')"
    $importOut = (& $VenvPython -c $importCode 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0 -or $importOut -notmatch "IMPORTS_OK") {
        Write-Fail "Import check failed:`n$importOut"
        exit 1
    }
    Write-Host "OK: critical modules import cleanly"
} finally {
    Pop-Location
}

# 7. Backend + health
Write-Step "Starting backend and waiting for health endpoint"
$backend = Start-Process -FilePath $VenvPython `
    -ArgumentList @("-m", "web_ui.backend.server") `
    -WorkingDirectory $Root `
    -PassThru `
    -NoNewWindow `
    -RedirectStandardOutput (Join-Path $Root "smoke_backend.out.log") `
    -RedirectStandardError (Join-Path $Root "smoke_backend.err.log")
try {
    $healthy = $false
    for ($i = 0; $i -lt 60; $i++) {
        try {
            $resp = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/health" -UseBasicParsing -TimeoutSec 2
            if ($resp.StatusCode -eq 200) {
                $healthy = $true
                break
            }
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    if ($healthy) {
        Write-Host "OK: backend healthy (http://127.0.0.1:8000/api/health)"
    } else {
        Write-Fail "Backend did not become healthy within 60s."
        if (Test-Path (Join-Path $Root "smoke_backend.err.log")) {
            Get-Content (Join-Path $Root "smoke_backend.err.log") -Tail 20 | ForEach-Object { Write-Host "  $_" }
        }
        exit 1
    }
} finally {
    if ($backend -and -not $backend.HasExited) {
        Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue
    }
}

# 8. Summary
Write-Step "Smoke test finished"
if ($script:failures -gt 0) {
    Write-Host "FAIL: $($script:failures) check(s) failed" -ForegroundColor Red
    exit 1
}
Write-Host "OK: all checks passed"
exit 0
