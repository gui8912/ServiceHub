@echo off
chcp 65001 >nul
setlocal
set "PS1=%~dp0svc.ps1"
if not exist "%PS1%" (
  echo [ERROR] svc.ps1 not found: %PS1%
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
exit /b %ERRORLEVEL%
