@echo off
setlocal
rem TEMP_LATENCY_DIAGNOSTIC: admin-only collector; remove after the incident.
set "TARGET=%~1"
if "%TARGET%"=="" (
  set /p "TARGET=Enter the actual TS domain (for example www.ts01.scjrdomain.com): "
)
if "%TARGET%"=="" (
  echo TargetHost is required.
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0collect_latency_diagnostics.ps1" -TargetHost "%TARGET%"
endlocal
