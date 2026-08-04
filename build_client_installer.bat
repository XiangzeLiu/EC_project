@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "APP_DISPLAY_NAME=SC Client"
set "APP_EXE_NAME=SCClient"
set "PLATFORM_ID=0"
set "ROOT_DIR=%cd%"
set "ENTRY_FILE=%ROOT_DIR%\Client\main.py"
set "FONT_DIR=%ROOT_DIR%\Client\assets\fonts"
set "ICON_DIR=%ROOT_DIR%\Client\assets\icons"
set "BUILD_REQUIREMENTS=%ROOT_DIR%\Client\requirements-build.txt"
set "DIST_ROOT=%ROOT_DIR%\dist\ClientInstaller"
set "APP_DIST=%DIST_ROOT%\app"
set "INSTALLER_OUT=%DIST_ROOT%\installer"
set "BUILD_ROOT=%ROOT_DIR%\build\ClientInstaller"
set "SPEC_DIR=%BUILD_ROOT%\spec"
set "WORK_DIR=%BUILD_ROOT%\work"
set "APP_OUT=%APP_DIST%\%APP_EXE_NAME%"
set "GENERATED_DIR=%BUILD_ROOT%\generated"
set "BUILD_INFO=%GENERATED_DIR%\client_build_info.json"
set "CHECKSUM_FILE=%DIST_ROOT%\SHA256SUMS.txt"
set "ISS_FILE=%BUILD_ROOT%\%APP_EXE_NAME%.iss"
set "BUILD_VENV=%LOCALAPPDATA%\SCClientBuild\venv"
set "BUILD_PY=%BUILD_VENV%\Scripts\python.exe"
set "INSTALLER_FILE="

echo [Client Package] Root: %ROOT_DIR%

if not exist "%ENTRY_FILE%" (
  echo [Client Package] ERROR: missing Client entry: %ENTRY_FILE%
  goto :fail
)
if not exist "%FONT_DIR%\Inter-Variable.ttf" (
  echo [Client Package] ERROR: missing Inter font.
  goto :fail
)
if not exist "%FONT_DIR%\JetBrainsMono-Variable.ttf" (
  echo [Client Package] ERROR: missing JetBrains Mono font.
  goto :fail
)
if not exist "%ICON_DIR%\search.svg" (
  echo [Client Package] ERROR: missing Client search icon.
  goto :fail
)
if not exist "%BUILD_REQUIREMENTS%" (
  echo [Client Package] ERROR: missing pinned build requirements: %BUILD_REQUIREMENTS%
  goto :fail
)

set "ISCC_EXE="
for %%P in ("%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe") do (
  if exist "%%~P" set "ISCC_EXE=%%~P"
)
if "%ISCC_EXE%"=="" (
  for /f "delims=" %%I in ('where ISCC.exe 2^>nul') do (
    if "%ISCC_EXE%"=="" set "ISCC_EXE=%%I"
  )
)
if "%ISCC_EXE%"=="" if /I not "%ALLOW_PORTABLE_ONLY%"=="1" echo [Client Package] ERROR: Inno Setup 6 was not found. Install it before building the Client installer.
if "%ISCC_EXE%"=="" if /I not "%ALLOW_PORTABLE_ONLY%"=="1" goto :fail

set "PY_CMD=%CLIENT_BUILD_PY%"
if not defined PY_CMD (
  where py >nul 2>nul
  if errorlevel 1 (
    echo [Client Package] Python Launcher was not found. Install Python 3.11 x64 first.
    goto :fail
  )
  if not exist "%BUILD_PY%" (
    echo [Client Package] Creating isolated Python 3.11 build environment...
    py -3.11 -m venv "%BUILD_VENV%"
    if errorlevel 1 goto :fail
  )
  set "PY_CMD=%BUILD_PY%"
)

if not exist "%PY_CMD%" (
  echo [Client Package] ERROR: build Python was not found: %PY_CMD%
  goto :fail
)
echo [Client Package] Python: %PY_CMD%

"%PY_CMD%" -c "import struct,sys; assert sys.version_info[:2] == (3, 11), sys.version; assert struct.calcsize('P') * 8 == 64, '64-bit Python required'; print(sys.version)"
if errorlevel 1 (
  echo [Client Package] ERROR: use Python 3.11 64-bit to build the Client.
  goto :fail
)

if /I not "%SKIP_PIP%"=="1" (
  echo [Client Package] Installing pinned Client build dependencies...
  "%PY_CMD%" -m pip install --disable-pip-version-check -r "%BUILD_REQUIREMENTS%"
  if errorlevel 1 goto :fail
) else (
  echo [Client Package] SKIP_PIP=1, dependency installation skipped.
)

echo [Client Package] Verifying runtime dependencies...
"%PY_CMD%" -c "import PyInstaller,PySide6,websockets,tzdata; print(PyInstaller.__version__, PySide6.__version__, websockets.__version__, tzdata.__version__)"
if errorlevel 1 goto :fail

set "BUILD_TIMESTAMP=%CLIENT_BUILD_TIMESTAMP%"
if not defined BUILD_TIMESTAMP (
  for /f "delims=" %%I in ('powershell -NoProfile -Command "$tz=[TimeZoneInfo]::FindSystemTimeZoneById('China Standard Time'); [TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow,$tz).ToString('yyyyMMddHHmmss')"') do if not defined BUILD_TIMESTAMP set "BUILD_TIMESTAMP=%%I"
)
powershell -NoProfile -Command "if ('%BUILD_TIMESTAMP%' -notmatch '^\d{14}$') { exit 1 }"
if errorlevel 1 (
  echo [Client Package] ERROR: invalid Beijing build timestamp: %BUILD_TIMESTAMP%
  goto :fail
)
set "APP_VERSION=v_%PLATFORM_ID%_%BUILD_TIMESTAMP%"
echo [Client Package] Version: %APP_VERSION%

echo [Client Package] Cleaning previous Client package output...
if exist "%DIST_ROOT%" rmdir /s /q "%DIST_ROOT%"
if exist "%BUILD_ROOT%" rmdir /s /q "%BUILD_ROOT%"
mkdir "%APP_DIST%" >nul 2>nul
mkdir "%BUILD_ROOT%" >nul 2>nul
mkdir "%GENERATED_DIR%" >nul 2>nul
mkdir "%INSTALLER_OUT%" >nul 2>nul

echo {"platform":%PLATFORM_ID%,"build_timestamp":"%BUILD_TIMESTAMP%","version":"%APP_VERSION%"} > "%BUILD_INFO%"
if not exist "%BUILD_INFO%" (
  echo [Client Package] ERROR: failed to create build metadata.
  goto :fail
)

echo [Client Package] Building portable application...
"%PY_CMD%" -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onedir ^
  --windowed ^
  --disable-windowed-traceback ^
  --name "%APP_EXE_NAME%" ^
  --distpath "%APP_DIST%" ^
  --workpath "%WORK_DIR%" ^
  --specpath "%SPEC_DIR%" ^
  --paths "%ROOT_DIR%" ^
  --add-data "%FONT_DIR%;Client\assets\fonts" ^
  --add-data "%ICON_DIR%;Client\assets\icons" ^
  --add-data "%BUILD_INFO%;Client" ^
  --collect-all tzdata ^
  --collect-all websockets ^
  "%ENTRY_FILE%"
if errorlevel 1 goto :fail

if not exist "%APP_OUT%\%APP_EXE_NAME%.exe" (
  echo [Client Package] ERROR: PyInstaller did not create %APP_EXE_NAME%.exe.
  goto :fail
)

echo [Client Package] Running packaged application self-test...
set "QT_QPA_PLATFORM=offscreen"
"%APP_OUT%\%APP_EXE_NAME%.exe" --package-self-test
set "SELFTEST_RESULT=%ERRORLEVEL%"
set "QT_QPA_PLATFORM="
if not "%SELFTEST_RESULT%"=="0" (
  echo [Client Package] ERROR: packaged application self-test failed.
  goto :fail
)

echo [Client Package] Checking package for local-only files...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$blockedNames=@('.tt_config.json','.env','.env.production','server_manager.db','server_manager.db-wal','server_manager.db-shm'); $blockedExt=@('.log','.db','.sqlite','.pyc','.py','.md','.rtf'); $bad=Get-ChildItem -LiteralPath '%APP_OUT%' -Recurse -Force -ErrorAction SilentlyContinue | Where-Object { ($blockedNames -contains $_.Name) -or ($blockedExt -contains $_.Extension.ToLowerInvariant()) -or ($_.FullName -match '\\(data|logs)\\') }; if($bad){Write-Host '[Client Package] ERROR: local-only files found:'; $bad.FullName; exit 1}"
if errorlevel 1 goto :fail

echo [Client Package] Creating portable ZIP...
set "PORTABLE_ZIP=%DIST_ROOT%\SC_Client_Portable_%APP_VERSION%.zip"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; Compress-Archive -LiteralPath '%APP_OUT%' -DestinationPath '%PORTABLE_ZIP%' -Force"
if errorlevel 1 goto :fail
if not exist "%PORTABLE_ZIP%" goto :fail

if "%ISCC_EXE%"=="" (
  echo [Client Package] Inno Setup 6 not found; portable-only mode was explicitly enabled.
  goto :finalize
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
  echo AppPublisher=SC Project
  echo MinVersion=10.0
  echo ArchitecturesAllowed=x64compatible
  echo ArchitecturesInstallIn64BitMode=x64compatible
  echo DefaultDirName={localappdata}\Programs\SC Client
  echo DefaultGroupName={#MyAppName}
  echo DisableProgramGroupPage=yes
  echo OutputDir=%INSTALLER_OUT%
  echo OutputBaseFilename=SC_Client_Setup_%APP_VERSION%
  echo Compression=lzma
  echo SolidCompression=yes
  echo WizardStyle=modern
  echo PrivilegesRequired=lowest
  echo CloseApplications=yes
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
set "INSTALLER_FILE=%INSTALLER_OUT%\SC_Client_Setup_%APP_VERSION%.exe"
if not exist "%INSTALLER_FILE%" (
  echo [Client Package] ERROR: installer output was not created.
  goto :fail
)

:finalize
echo [Client Package] Creating SHA-256 checksums...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $files=@('%PORTABLE_ZIP%'); $installer='%INSTALLER_FILE%'; if($installer -and (Test-Path -LiteralPath $installer)){$files += $installer}; $lines=$files | ForEach-Object { $hash=(Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash; '{0}  {1}' -f $hash,(Split-Path $_ -Leaf) }; $lines | Set-Content -LiteralPath '%CHECKSUM_FILE%' -Encoding ASCII"
if errorlevel 1 goto :fail
if not exist "%CHECKSUM_FILE%" goto :fail

echo [Client Package] Portable app: %APP_OUT%\%APP_EXE_NAME%.exe
echo [Client Package] Portable ZIP: %PORTABLE_ZIP%
if defined INSTALLER_FILE echo [Client Package] Installer: %INSTALLER_FILE%
echo [Client Package] Checksums: %CHECKSUM_FILE%
goto :success

:success
echo [Client Package] Done.
if /I not "%NO_PAUSE%"=="1" pause
endlocal
exit /b 0

:fail
echo [Client Package] FAILED.
if /I not "%NO_PAUSE%"=="1" pause
endlocal
exit /b 1
