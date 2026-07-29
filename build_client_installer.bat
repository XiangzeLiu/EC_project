@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "APP_DISPLAY_NAME=EC Client"
set "APP_EXE_NAME=ECClient"
set "APP_VERSION=1.0.0"

if not "%CLIENT_BUILD_VERSION%"=="" set "APP_VERSION=%CLIENT_BUILD_VERSION%"

set "ROOT_DIR=%cd%"
set "ENTRY_FILE=%ROOT_DIR%\Client\main.py"
set "FONT_DIR=%ROOT_DIR%\Client\assets\fonts"
set "DIST_ROOT=%ROOT_DIR%\dist\ClientInstaller"
set "APP_DIST=%DIST_ROOT%\app"
set "PORTABLE_ZIP=%DIST_ROOT%\EC_Client_Portable_%APP_VERSION%.zip"
set "INSTALLER_OUT=%DIST_ROOT%\installer"
set "BUILD_ROOT=%ROOT_DIR%\build\ClientInstaller"
set "SPEC_DIR=%BUILD_ROOT%\spec"
set "WORK_DIR=%BUILD_ROOT%\work"
set "APP_OUT=%APP_DIST%\%APP_EXE_NAME%"
set "ISS_FILE=%BUILD_ROOT%\ECClient.iss"

echo [Client Package] Root: %ROOT_DIR%

if not exist "%ENTRY_FILE%" (
  echo [Client Package] ERROR: missing Client entry: %ENTRY_FILE%
  goto :fail
)

if not exist "%FONT_DIR%" (
  echo [Client Package] ERROR: missing font directory: %FONT_DIR%
  goto :fail
)

set "PY_CMD="
if exist "%ROOT_DIR%\.venv\Scripts\python.exe" (
  set "PY_CMD=%ROOT_DIR%\.venv\Scripts\python.exe"
) else (
  where py >nul 2>nul
  if not errorlevel 1 (
    set "PY_CMD=py -3"
  ) else (
    where python >nul 2>nul
    if not errorlevel 1 set "PY_CMD=python"
  )
)

if "%PY_CMD%"=="" (
  echo [Client Package] ERROR: Python was not found.
  goto :fail
)

echo [Client Package] Python: %PY_CMD%

if /I not "%SKIP_PIP%"=="1" (
  echo [Client Package] Checking PyInstaller...
  %PY_CMD% -c "import PyInstaller" >nul 2>nul || %PY_CMD% -m pip install -U pyinstaller
  if errorlevel 1 goto :fail

  if exist "%ROOT_DIR%\Client\requirements.txt" (
    echo [Client Package] Installing Client requirements...
    %PY_CMD% -m pip install -r "%ROOT_DIR%\Client\requirements.txt"
    if errorlevel 1 goto :fail
  )
) else (
  echo [Client Package] SKIP_PIP=1, dependency installation skipped.
)

echo [Client Package] Verifying runtime dependencies...
%PY_CMD% -c "import PyInstaller, PySide6, websockets, tzdata; print('ok')" >nul
if errorlevel 1 goto :fail

echo [Client Package] Cleaning previous Client package output...
if exist "%DIST_ROOT%" rmdir /s /q "%DIST_ROOT%"
if exist "%BUILD_ROOT%" rmdir /s /q "%BUILD_ROOT%"
mkdir "%APP_DIST%" >nul 2>nul
mkdir "%BUILD_ROOT%" >nul 2>nul
mkdir "%INSTALLER_OUT%" >nul 2>nul

echo [Client Package] Building portable application...
%PY_CMD% -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onedir ^
  --windowed ^
  --name "%APP_EXE_NAME%" ^
  --distpath "%APP_DIST%" ^
  --workpath "%WORK_DIR%" ^
  --specpath "%SPEC_DIR%" ^
  --paths "%ROOT_DIR%" ^
  --add-data "%FONT_DIR%;Client\assets\fonts" ^
  --collect-all tzdata ^
  --collect-all websockets ^
  "%ENTRY_FILE%"
if errorlevel 1 goto :fail

if not exist "%APP_OUT%\%APP_EXE_NAME%.exe" (
  echo [Client Package] ERROR: PyInstaller did not create %APP_EXE_NAME%.exe.
  goto :fail
)

echo [Client Package] Checking package for local-only files...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$bad = Get-ChildItem -LiteralPath '%APP_OUT%' -Recurse -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq '.tt_config.json' -or $_.Name -like 'Client_*.md' }; if ($bad) { Write-Host '[Client Package] ERROR: unexpected local-only files found:'; $bad.FullName; exit 1 }"
if errorlevel 1 goto :fail

echo [Client Package] Creating portable ZIP...
if exist "%PORTABLE_ZIP%" del /f /q "%PORTABLE_ZIP%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference = 'Stop'; Compress-Archive -LiteralPath '%APP_OUT%' -DestinationPath '%PORTABLE_ZIP%' -Force"
if errorlevel 1 goto :fail
if not exist "%PORTABLE_ZIP%" goto :fail
powershell -NoProfile -ExecutionPolicy Bypass -Command "$zip = Get-Item -LiteralPath '%PORTABLE_ZIP%' -ErrorAction Stop; if ($zip.Length -le 0) { exit 1 }"
if errorlevel 1 goto :fail

set "ISCC_EXE="
for %%P in ("%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe") do (
  if exist "%%~P" set "ISCC_EXE=%%~P"
)
if "%ISCC_EXE%"=="" (
  for /f "delims=" %%I in ('where ISCC.exe 2^>nul') do (
    if "%ISCC_EXE%"=="" set "ISCC_EXE=%%I"
  )
)

if "%ISCC_EXE%"=="" (
  echo [Client Package] Inno Setup 6 was not found. Installer build skipped.
  echo [Client Package] Portable app: %APP_OUT%\%APP_EXE_NAME%.exe
  echo [Client Package] Portable ZIP: %PORTABLE_ZIP%
  echo [Client Package] Install Inno Setup 6 and run this script again to create an installer.
  goto :success
)

echo [Client Package] Creating Inno Setup script...
(
  echo #define MyAppName "%APP_DISPLAY_NAME%"
  echo #define MyAppExeName "%APP_EXE_NAME%.exe"
  echo #define MyAppVersion "%APP_VERSION%"
  echo.
  echo [Setup]
  echo AppId={{7315B8B0-8D5F-455A-909E-09A4249F5440}
  echo AppName={#MyAppName}
  echo AppVersion={#MyAppVersion}
  echo AppPublisher=EC Project
  echo DefaultDirName={localappdata}\Programs\EC Client
  echo DefaultGroupName={#MyAppName}
  echo DisableProgramGroupPage=yes
  echo OutputDir=%INSTALLER_OUT%
  echo OutputBaseFilename=EC_Client_Setup_%APP_VERSION%
  echo Compression=lzma
  echo SolidCompression=yes
  echo WizardStyle=modern
  echo PrivilegesRequired=lowest
  echo UninstallDisplayName={#MyAppName}
  echo.
  echo [Languages]
  echo Name: "english"; MessagesFile: "compiler:Default.isl"
  echo.
  echo [Tasks]
  echo Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"
  echo.
  echo [Files]
  echo Source: "%APP_OUT%\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
  echo.
  echo [Icons]
  echo Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
  echo Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
  echo.
  echo [Run]
  echo Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
) > "%ISS_FILE%"

echo [Client Package] Building installer with Inno Setup...
"%ISCC_EXE%" "%ISS_FILE%"
if errorlevel 1 goto :fail

echo [Client Package] Installer output: %INSTALLER_OUT%\EC_Client_Setup_%APP_VERSION%.exe

:success
echo [Client Package] Done.
endlocal
exit /b 0

:fail
echo [Client Package] FAILED.
endlocal
exit /b 1
