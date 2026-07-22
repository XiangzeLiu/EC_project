@echo off
setlocal
cd /d "%~dp0"

if exist "%~dp0ts.local.bat" call "%~dp0ts.local.bat"

if not defined TS_MANAGER_URL set "TS_MANAGER_URL=https://scjrdomain.com"
if not defined TS_BIND_HOST set "TS_BIND_HOST=127.0.0.1"
if not defined TS_WS_PORT set "TS_WS_PORT=8900"
if not defined TS_CADDY_AUTO_MANAGE set "TS_CADDY_AUTO_MANAGE=1"
if not defined TS_CADDY_REQUIRED set "TS_CADDY_REQUIRED=1"
if not defined TS_CADDY_EXE set "TS_CADDY_EXE=%~dp0caddy\caddy.exe"
if not defined TS_CADDY_DIR set "TS_CADDY_DIR=%~dp0caddy"
if not defined TS_CADDY_ADMIN set "TS_CADDY_ADMIN=127.0.0.1:2020"
if not defined TS_CADDY_START_TIMEOUT set "TS_CADDY_START_TIMEOUT=10"

if not exist "%~dp0TraderServer.exe" goto :app_missing
"%~dp0TraderServer.exe"
exit /b %errorlevel%

:app_missing
echo [TS] TraderServer.exe was not found.
exit /b 1
