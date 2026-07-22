@echo off
setlocal
cd /d "%~dp0"

echo [SM] 开始打包...

set "PY_CMD="
where py >nul 2>nul
if %errorlevel%==0 (
  set "PY_CMD=py -3"
) else (
  set "PY_CMD=python"
)

echo [SM] 使用解释器: %PY_CMD%

if not exist "%cd%\Server_manager\caddy\caddy.exe" goto :caddy_missing

%PY_CMD% -c "import PyInstaller" >nul 2>nul || %PY_CMD% -m pip install -U pyinstaller
if errorlevel 1 goto :fail

echo [SM] 安装基础依赖...
%PY_CMD% -m pip install -U fastapi "uvicorn[standard]" starlette jinja2 pydantic certifi python-multipart tencentcloud-sdk-python
if errorlevel 1 goto :fail

if exist "%cd%\Server_manager\requirements.txt" (
  echo [SM] 尝试安装可选依赖: Server_manager\requirements.txt
  %PY_CMD% -m pip install -r "%cd%\Server_manager\requirements.txt"
  if errorlevel 1 (
    echo [SM] 可选依赖安装失败，已忽略并继续打包。
  )
)

%PY_CMD% -c "import fastapi,starlette,uvicorn,jinja2,pydantic,certifi,multipart" >nul 2>nul
if errorlevel 1 (
  echo [SM] 基础依赖校验失败。
  goto :fail
)

%PY_CMD% -m PyInstaller --noconfirm --clean --onedir --name ServerManager --distpath "%cd%\dist\ServerManager" --workpath "%cd%\build\ServerManager" --specpath "%cd%\build\ServerManager" --paths "%cd%" --paths "%cd%\Server_manager" --add-data "%cd%\Server_manager\templates;Server_manager\templates" --add-data "%cd%\Server_manager\admin.json;Server_manager" --add-data "%cd%\Server_manager\users.json;Server_manager" --collect-all Server_manager --collect-all fastapi --collect-all starlette --collect-all uvicorn --collect-all tencentcloud "%cd%\Server_manager\main.py"
if errorlevel 1 goto :fail

set "SM_OUT=%cd%\dist\ServerManager\ServerManager"
if not exist "%SM_OUT%\caddy" mkdir "%SM_OUT%\caddy"
copy /Y "%cd%\deploy\caddy\Caddyfile.sm.example" "%SM_OUT%\caddy\Caddyfile" >nul
copy /Y "%cd%\deploy\windows\start_sm.bat" "%SM_OUT%\start_sm.bat" >nul
copy /Y "%cd%\deploy\windows\sm.env.example" "%SM_OUT%\sm.env.example" >nul
copy /Y "%cd%\deploy\windows\sm.local.bat.example" "%SM_OUT%\sm.local.bat.example" >nul
copy /Y "%cd%\deploy\caddy\CADDY_VERSION.txt" "%SM_OUT%\caddy\CADDY_VERSION.txt" >nul
copy /Y "%cd%\Server_manager\caddy\caddy.exe" "%SM_OUT%\caddy\caddy.exe" >nul
if errorlevel 1 goto :fail

echo [SM] 打包完成：%cd%\dist\ServerManager\ServerManager\ServerManager.exe
endlocal
exit /b 0

:caddy_missing
echo [SM] 缺少 Server_manager\caddy\caddy.exe，无法生成生产包。
endlocal
exit /b 1

:fail
echo [SM] 打包失败。
endlocal
exit /b 1
