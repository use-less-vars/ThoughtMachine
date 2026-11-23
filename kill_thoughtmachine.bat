@echo off
REM==============================================================================
REM kill_thoughtmachine.bat
REM
REM  ! SYNCED with kill_thoughtmachine.sh - keep in agreement.
REM  ! If you edit this file, mirror the same change in the shell script.
REM==============================================================================
REM  Forcefully stop all ThoughtMachine processes.
REM
REM  Kills:
REM    - Python processes running the web backend (port 8000)
REM    - Vite dev server processes (ports 5173-5177)
REM    - Any python.exe processes related to ThoughtMachine
REM
REM  Safe to run even when nothing is running (errors suppressed).
REM==============================================================================

setlocal enabledelayedexpansion
echo Killing ThoughtMachine processes...

REM -- Crash receipt: name the listeners BEFORE killing anything -----------------
REM Writes one kill_switch record (ISO-8601 timestamp, pids, command lines) to
REM logs\backend_death.log, resolved from THIS script's own directory.
set "TM_DEATH_LOG_DIR=%~dp0logs"
if not exist "%TM_DEATH_LOG_DIR%" mkdir "%TM_DEATH_LOG_DIR%" >nul 2>&1
set "TM_KILL_PIDS="
for /f "usebackq delims=" %%r in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$ports=5173,5174,5175,5176,5177,8000;$rows=@();foreach($p in $ports){$c=Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue|Where-Object{$_.State -eq 'Listen'}|Select-Object -First 1;if($c){$cl='(gone)';$pr=Get-CimInstance Win32_Process -Filter ('ProcessId=' + $c.OwningProcess) -ErrorAction SilentlyContinue;if($pr){$cl=$pr.CommandLine};$rows+=,(@([string]$c.OwningProcess,$cl))}};$pids=($rows|ForEach-Object{$_[0]}) -join ',';$cmds=($rows|ForEach-Object{$_[1]}) -join ';';if($pids -eq ''){$pids='NONE'};$ts=(Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz');Add-Content -LiteralPath '%TM_DEATH_LOG_DIR%\backend_death.log' -Value ('kill_switch|' + $ts + '|pids=' + $pids + '|cmds=' + $cmds) -Encoding ASCII;Write-Output $pids"`) do (
    set "TM_KILL_PIDS=%%r"
)
if not defined TM_KILL_PIDS set "TM_KILL_PIDS=UNKNOWN"

REM -- Kill Vite dev servers (ports 5173-5177) ----------------------------------
for %%p in (5173 5174 5175 5176 5177) do (
    REM Single-line PowerShell - avoids batch multiline parsing errors
    powershell -Command "$p=Get-NetTCPConnection -LocalPort %%p -ErrorAction SilentlyContinue|Where-Object{$_.State -eq 'Listen'}|Select-Object -First 1;if($p){Stop-Process -Id $p.OwningProcess -Force -ErrorAction SilentlyContinue}" 2>nul
    if not errorlevel 1 (
        echo   Killed process using port %%p
    )
)

REM -- Kill Python backend (port 8000) ------------------------------------------
powershell -Command "$p=Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue|Where-Object{$_.State -eq 'Listen'}|Select-Object -First 1;if($p){Stop-Process -Id $p.OwningProcess -Force -ErrorAction SilentlyContinue}" 2>nul
if not errorlevel 1 (
    echo   Killed python backend using port 8000
)

REM -- Kill any remaining thoughtmachine-related python.exe ---------------------
REM (catches processes that may not have an active listening port)
taskkill /f /fi "WINDOWTITLE eq ThoughtMachine Backend*" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq Vite Dev Server*" >nul 2>&1

REM -- Also try by window title (for start "" /wait windows) --------------------
taskkill /f /fi "WINDOWTITLE eq Administrator:  ThoughtMachine Backend*" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq Administrator:  Vite Dev Server*" >nul 2>&1

if /i "%TM_KILL_PIDS%"=="NONE" (
    echo No ThoughtMachine process was listening on ports 5173-5177 / 8000 - nothing to stop.
) else if /i "%TM_KILL_PIDS%"=="UNKNOWN" (
    echo Could not enumerate the listeners before killing - the kill still ran.
) else (
    echo All ThoughtMachine processes stopped.
    echo   Receipt: %TM_DEATH_LOG_DIR%\backend_death.log  pids=%TM_KILL_PIDS%
)
