@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not defined SERVER_BUILD_TIMESTAMP (
  for /f "delims=" %%I in ('powershell -NoProfile -Command "$tz=[TimeZoneInfo]::FindSystemTimeZoneById('China Standard Time'); [TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow,$tz).ToString('yyyyMMddHHmmss')"') do if not defined SERVER_BUILD_TIMESTAMP set "SERVER_BUILD_TIMESTAMP=%%I"
)
if not defined SERVER_BUILD_TIMESTAMP goto :fail

set "NO_PAUSE=1"
call "%~dp0build_sm.bat"
if errorlevel 1 goto :fail
call "%~dp0build_ts.bat"
if errorlevel 1 goto :fail

echo [Server Packages] SM and TS packages completed.
endlocal & exit /b 0

:fail
echo [Server Packages] FAILED.
endlocal & exit /b 1
