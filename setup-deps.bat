@echo off
rem ==========================================================================
rem  ServiceHub - install bundled python packages into runtime\site-packages
rem  Run once after cloning the repo on a new machine (the launcher calls it
rem  automatically when psutil is missing, so double clicking is optional).
rem  Usage:  setup-deps.bat          install / upgrade
rem          setup-deps.bat /quiet   no pause at the end (called by launcher)
rem ==========================================================================
setlocal DisableDelayedExpansion
cd /d "%~dp0"
if not exist "%~dp0runtime" mkdir "%~dp0runtime"

set "QUIET=0"
if /i "%~1"=="/quiet" set "QUIET=1"

rem ---- locate Python 3 (same order as the launcher)
set "PY="
if exist "%~dp0runtime\python.txt" set /p PY=<"%~dp0runtime\python.txt"
if defined PY if not exist "%PY%" set "PY="
if not defined PY for /f "usebackq delims=" %%I in (`py -3 -c "import sys;print(sys.executable)" 2^>nul`) do set "PY=%%I"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY if exist "C:\Program Files\Python312\python.exe" set "PY=C:\Program Files\Python312\python.exe"
if not defined PY for /f "usebackq delims=" %%I in (`python -c "import sys;print(sys.executable)" 2^>nul`) do set "PY=%%I"

if not defined PY (
  echo [ERROR] Python 3 not found on this machine.
  echo         Install Python 3.10+ from https://www.python.org/downloads/
  echo         and tick "Add python.exe to PATH", then run this file again.
  if "%QUIET%"=="0" pause
  exit /b 1
)

echo [INFO] python : %PY%
"%PY%" -c "import sys; print('[INFO] version: ' + sys.version.split()[0])"
if errorlevel 1 (
  echo [ERROR] That interpreter does not run. Install a fresh Python 3.
  if "%QUIET%"=="0" pause
  exit /b 1
)

rem remember the interpreter: psutil / Pillow ship per-version binary wheels
>"%~dp0runtime\python.txt" echo %PY%

echo [INFO] installing psutil / pystray / Pillow into runtime\site-packages ...
"%PY%" -m pip install --disable-pip-version-check --no-warn-script-location --target "%~dp0runtime\site-packages" --upgrade --no-compile psutil pystray Pillow
if errorlevel 1 (
  echo.
  echo [ERROR] pip install failed. Things to try:
  echo         1^) "%PY%" -m pip install --upgrade pip
  echo         2^) if the network needs a proxy, set HTTPS_PROXY and retry
  if "%QUIET%"=="0" pause
  exit /b 1
)

echo [INFO] verifying imports ...
"%PY%" -c "import sys; sys.path.insert(0, r'%~dp0runtime\site-packages'); import psutil, pystray, PIL; print('[OK] psutil ' + psutil.__version__ + ' | Pillow ' + PIL.__version__)"
if errorlevel 1 (
  echo [ERROR] Packages installed but import failed. Delete runtime\site-packages and retry.
  if "%QUIET%"=="0" pause
  exit /b 1
)

echo.
echo [OK] Dependencies are ready. Double click the launcher bat to start the hub.
if "%QUIET%"=="0" pause
exit /b 0
