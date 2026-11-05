@echo off
rem ==========================================================================
rem  ServiceHub - local services workbench (portable launcher)
rem
rem  Double click this file: the hub starts, stays in the tray, and opens the
rem  panel in the browser chosen in Settings (default: system browser).
rem  If an instance is already running it just re-opens the panel.
rem
rem  First run on a new machine: missing packages are installed automatically
rem  into runtime\site-packages (needs Python 3.10+). See setup-deps.bat.
rem
rem  Options:  --selftest   run diagnostics, print report, no browser window
rem ==========================================================================
setlocal DisableDelayedExpansion
cd /d "%~dp0"

rem ---- locate Python 3
rem  0) the interpreter that installed the packages (psutil ships binary
rem     extensions, so a different minor version cannot import them)
set "PY="
if exist "%~dp0runtime\python.txt" set /p PY=<"%~dp0runtime\python.txt"
if defined PY if not exist "%PY%" set "PY="
rem  1) the official py launcher: always points at a real system install
if not defined PY for /f "usebackq delims=" %%I in (`py -3 -c "import sys;print(sys.executable)" 2^>nul`) do set "PY=%%I"
rem  2) common per-user install dirs
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY if exist "C:\Program Files\Python312\python.exe" set "PY=C:\Program Files\Python312\python.exe"
rem  3) whatever "python" resolves to (last resort)
if not defined PY for /f "usebackq delims=" %%I in (`python -c "import sys;print(sys.executable)" 2^>nul`) do set "PY=%%I"

if not defined PY (
  echo [ERROR] Python 3 not found on this machine.
  echo         Install Python 3.10+ from https://www.python.org/downloads/
  echo         and tick "Add python.exe to PATH", then run this file again.
  pause
  exit /b 1
)

set "PYW=%PY:python.exe=pythonw.exe%"
if not exist "%PYW%" set "PYW=%PY%"
if not exist "%~dp0ServiceHub.py" (
  echo [ERROR] ServiceHub.py not found in %~dp0
  pause
  exit /b 1
)

rem ---- dependencies live in runtime\site-packages, not in git
"%PY%" -c "import sys;sys.path.insert(0,r'%~dp0runtime\site-packages');import psutil" >nul 2>nul
if errorlevel 1 (
  echo [SETUP] Installing psutil / pystray / Pillow into runtime\site-packages ...
  call "%~dp0setup-deps.bat" /quiet
  if errorlevel 1 (
    echo.
    echo [ERROR] Dependency install failed. Run setup-deps.bat manually to see why.
    pause
    exit /b 1
  )
)

if /i "%~1"=="--selftest" (
  chcp 65001 >nul
  "%PY%" -X utf8 "%~dp0ServiceHub.py" --selftest
  echo.
  echo Report written to logs\selftest-report.txt
  pause
  exit /b 0
)

rem Start the resident hub. The hub opens the panel itself with the configured
rem browser, so this script must not run "start http://..." (double tab).
rem modified: remember and clear stale port.txt first; a crashed previous run
rem otherwise makes this script report an old port immediately.
set "OLDPORT="
if exist "%~dp0runtime\port.txt" set /p OLDPORT=<"%~dp0runtime\port.txt"
if exist "%~dp0runtime\port.txt" del /q "%~dp0runtime\port.txt" >nul 2>nul
start "ServiceHub" "%PYW%" "%~dp0ServiceHub.py" --port 9500

rem Wait at most 15 seconds for runtime\port.txt as a liveness check
set HUBPORT=
for /l %%i in (1,1,15) do (
  if exist "%~dp0runtime\port.txt" (
    set /p HUBPORT=<"%~dp0runtime\port.txt"
    goto HAVE_PORT
  )
  timeout /t 1 /nobreak >nul
)
:HAVE_PORT
if defined HUBPORT (
  echo ServiceHub is running on port %HUBPORT%.
  echo If the panel did not open, visit http://127.0.0.1:%HUBPORT%/
) else (
  rem modified: an existing instance does not rewrite port.txt; restore it and say so.
  if defined OLDPORT (
    >"%~dp0runtime\port.txt" echo %OLDPORT%
    echo [WARN] No new port reported. ServiceHub may already be running on port %OLDPORT%.
    echo Open http://127.0.0.1:%OLDPORT%/ in your browser.
  ) else (
    echo [WARN] Hub did not report a port in 15 seconds.
    echo Check logs\hub-*.log, or run this file with:  --selftest
  )
)
exit /b 0
