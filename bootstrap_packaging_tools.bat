@echo off
setlocal EnableExtensions
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\bootstrap_windows_tools.ps1"
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" echo [Packaging Tools] FAILED.
if /I not "%NO_PAUSE%"=="1" pause
endlocal & exit /b %RESULT%
