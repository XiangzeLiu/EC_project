@echo off
setlocal
cd /d "%~dp0"

if exist "%~dp0sm.local.bat" call "%~dp0sm.local.bat"

if not defined SERVER_HOST set "SERVER_HOST=127.0.0.1"
if not defined SERVER_PORT set "SERVER_PORT=8800"
if not defined SM_PUBLIC_BASE_URL set "SM_PUBLIC_BASE_URL=https://scjrdomain.com"
if not defined SM_ALLOWED_HOSTS set "SM_ALLOWED_HOSTS=scjrdomain.com,127.0.0.1,localhost"
if not defined SM_CORS_ORIGINS set "SM_CORS_ORIGINS=https://scjrdomain.com"
if not defined SM_COOKIE_SECURE set "SM_COOKIE_SECURE=1"
if not defined SM_DOMAIN_ROOT set "SM_DOMAIN_ROOT=scjrdomain.com"
if not defined SM_DOMAIN_POOL_REQUIRED set "SM_DOMAIN_POOL_REQUIRED=1"
if not defined SM_DNSPOD_MODE set "SM_DNSPOD_MODE=real"
if not defined SM_CADDY_AUTO_MANAGE set "SM_CADDY_AUTO_MANAGE=1"
if not defined SM_CADDY_REQUIRED set "SM_CADDY_REQUIRED=1"
if not defined SM_CADDY_EXE set "SM_CADDY_EXE=%~dp0caddy\caddy.exe"
if not defined SM_CADDY_DIR set "SM_CADDY_DIR=%~dp0caddy"
if not defined SM_CADDY_ADMIN set "SM_CADDY_ADMIN=127.0.0.1:2019"
if not defined SM_CADDY_START_TIMEOUT set "SM_CADDY_START_TIMEOUT=10"
if not defined CLIENT_TOKEN_TTL_SECONDS set "CLIENT_TOKEN_TTL_SECONDS=86400"
if not defined SM_BOOTSTRAP_ADMIN_USERNAME set "SM_BOOTSTRAP_ADMIN_USERNAME=admin"
if not defined SM_BOOTSTRAP_ADMIN_PASSWORD set "SM_BOOTSTRAP_ADMIN_PASSWORD=admin123"
if not defined SM_FINANCE_ENABLED set "SM_FINANCE_ENABLED=1"
if not defined SM_FINANCE_RETENTION_MONTHS set "SM_FINANCE_RETENTION_MONTHS=3"
if not defined SM_FINANCE_CLEANUP_INTERVAL_SECONDS set "SM_FINANCE_CLEANUP_INTERVAL_SECONDS=21600"

if not exist "%~dp0ServerManager.exe" goto :app_missing
"%~dp0ServerManager.exe"
exit /b %errorlevel%

:app_missing
echo [SM] ServerManager.exe was not found.
exit /b 1
