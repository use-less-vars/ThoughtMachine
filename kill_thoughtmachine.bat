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

echo All ThoughtMachine processes stopped.
