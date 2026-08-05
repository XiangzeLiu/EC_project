@echo off
setlocal
rem TEMP_LATENCY_DIAGNOSTIC: admin-only collector; remove after the incident.
set "TARGET=%~1"
set "PRIVATE_IP=%~2"
set "DURATION=%~3"
if "%TARGET%"=="" (
  set /p "TARGET=Enter the actual TS domain (for example www.ts01.scjrdomain.com): "
)
if "%TARGET%"=="" (
  echo TargetHost is required.
  exit /b 1
)
if "%PRIVATE_IP%"=="" (
  set /p "PRIVATE_IP=Enter the TS private IPv4 address (for example 192.0.2.1): "
)
if "%PRIVATE_IP%"=="" (
  echo PrivateIp is required.
  exit /b 1
)
if "%DURATION%"=="" set "DURATION=600"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0collect_latency_diagnostics.ps1" -TargetHost "%TARGET%" -PrivateIp "%PRIVATE_IP%" -DurationSeconds %DURATION%
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" echo Collection finished. Hosts mapping was restored automatically.
if not "%EXIT_CODE%"=="0" echo Collection failed. Check the output folder and restore instructions.
pause
endlocal
exit /b %EXIT_CODE%
