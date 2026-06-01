@echo off
setlocal
cd /d "%~dp0"

echo [Client] 开始打包...

set "PY_CMD="
where py >nul 2>nul
if %errorlevel%==0 (
  set "PY_CMD=py -3"
) else (
  set "PY_CMD=python"
)

echo [Client] 使用解释器: %PY_CMD%

%PY_CMD% -c "import PyInstaller" >nul 2>nul || %PY_CMD% -m pip install -U pyinstaller
if errorlevel 1 goto :fail

if exist "%cd%\Client\requirements.txt" (
  echo [Client] 安装依赖: Client\requirements.txt
  %PY_CMD% -m pip install -r "%cd%\Client\requirements.txt"
  if errorlevel 1 goto :fail
)

%PY_CMD% -c "import websockets,zoneinfo,tzdata" >nul 2>nul
if errorlevel 1 (
  echo [Client] 关键依赖校验失败，尝试补装...
  %PY_CMD% -m pip install -U websockets tzdata
  if errorlevel 1 goto :fail
)

%PY_CMD% -m PyInstaller --noconfirm --clean --onedir --name Client --distpath "%cd%\dist\Client" --workpath "%cd%\build\Client" --specpath "%cd%\build\Client" --paths "%cd%" --collect-all Client --collect-all tzdata "%cd%\Client\main.py"
if errorlevel 1 goto :fail

echo [Client] 打包完成：%cd%\dist\Client\Client\Client.exe
endlocal
exit /b 0

:fail
echo [Client] 打包失败。
endlocal
exit /b 1
