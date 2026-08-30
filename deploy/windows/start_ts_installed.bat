@echo off
setlocal EnableExtensions
cd /d "%~dp0"

powershell -NoProfile -Command "$principal=New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent()); if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){exit 1}"
if errorlevel 1 (
  powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath '%~f0' -WorkingDirectory '%~dp0' -Verb RunAs"
  exit /b %errorlevel%
)

set "TS_RUNTIME_ROOT=%ProgramData%\SC\TraderServer"
if not defined TS_ENVIRONMENT set "TS_ENVIRONMENT=production"
if not defined TS_DATA_DIR set "TS_DATA_DIR=%TS_RUNTIME_ROOT%\data"
if not defined TS_MANAGER_URL set "TS_MANAGER_URL=https://scjrdomain.com"
if not defined TS_BIND_HOST set "TS_BIND_HOST=127.0.0.1"
if not defined TS_WS_PORT set "TS_WS_PORT=8900"
if not defined TS_CADDY_AUTO_MANAGE set "TS_CADDY_AUTO_MANAGE=1"
if not defined TS_CADDY_REQUIRED set "TS_CADDY_REQUIRED=1"
if not defined TS_CADDY_EXE set "TS_CADDY_EXE=%~dp0caddy\caddy.exe"
if not defined TS_CADDY_DIR set "TS_CADDY_DIR=%TS_RUNTIME_ROOT%\caddy"
if not defined TS_CADDY_ADMIN set "TS_CADDY_ADMIN=127.0.0.1:2020"
if not defined TS_CADDY_START_TIMEOUT set "TS_CADDY_START_TIMEOUT=10"
if not defined TS_FINANCE_ENABLED set "TS_FINANCE_ENABLED=1"
if not defined TS_FINANCE_INTERVAL_SECONDS set "TS_FINANCE_INTERVAL_SECONDS=900"
if not defined TS_FINANCE_REQUEST_TIMEOUT_SECONDS set "TS_FINANCE_REQUEST_TIMEOUT_SECONDS=12"

if exist "%TS_RUNTIME_ROOT%\ts.local.bat" call "%TS_RUNTIME_ROOT%\ts.local.bat"

if not exist "%~dp0TraderServer.exe" goto :app_missing
start "" /D "%~dp0" "%~dp0TraderServer.exe"
exit /b 0

:app_missing
echo [TS] TraderServer.exe was not found.
exit /b 1
