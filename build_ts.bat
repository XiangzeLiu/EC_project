@echo off
setlocal EnableExtensions
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\build_server.ps1" -Target ts
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" echo [TS Package] FAILED.
if /I not "%NO_PAUSE%"=="1" pause
endlocal & exit /b %RESULT%
