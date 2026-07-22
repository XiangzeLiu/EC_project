@echo off
setlocal
cd /d "%~dp0"

echo [TS] 开始打包...

set "PY_CMD="
where py >nul 2>nul
if %errorlevel%==0 (
  set "PY_CMD=py -3"
) else (
  set "PY_CMD=python"
)

echo [TS] 使用解释器: %PY_CMD%

if not exist "%cd%\Trader_Server\caddy\caddy.exe" goto :caddy_missing

%PY_CMD% -c "import PyInstaller" >nul 2>nul || %PY_CMD% -m pip install -U pyinstaller
if errorlevel 1 goto :fail

if exist "%cd%\Trader_Server\requirements.txt" (
  echo [TS] 安装依赖: Trader_Server\requirements.txt
  %PY_CMD% -m pip install -r "%cd%\Trader_Server\requirements.txt"
  if errorlevel 1 goto :fail
)

%PY_CMD% -c "import fastapi,starlette,uvicorn,websockets,pydantic,httpx,PySide6,tastytrade" >nul 2>nul
if errorlevel 1 (
  echo [TS] 关键依赖校验失败，尝试补装...
  %PY_CMD% -m pip install -U fastapi "uvicorn[standard]" websockets pydantic httpx starlette PySide6 "tastytrade>=12.3,<14"
  if errorlevel 1 goto :fail
)

%PY_CMD% -m PyInstaller --noconfirm --clean --onedir --name TraderServer --distpath "%cd%\dist\TraderServer" --workpath "%cd%\build\TraderServer" --specpath "%cd%\build\TraderServer" --paths "%cd%" --add-data "%cd%\Trader_Server\data;Trader_Server\data" --collect-all Trader_Server --collect-all fastapi --collect-all starlette --collect-all tastytrade --collect-all uvicorn "%cd%\Trader_Server\main.py"
if errorlevel 1 goto :fail

set "TS_OUT=%cd%\dist\TraderServer\TraderServer"
if not exist "%TS_OUT%\caddy" mkdir "%TS_OUT%\caddy"
copy /Y "%cd%\deploy\caddy\Caddyfile.ts.example" "%TS_OUT%\caddy\Caddyfile.example" >nul
copy /Y "%cd%\deploy\windows\start_ts.bat" "%TS_OUT%\start_ts.bat" >nul
copy /Y "%cd%\deploy\windows\ts.env.example" "%TS_OUT%\ts.env.example" >nul
copy /Y "%cd%\deploy\windows\ts.local.bat.example" "%TS_OUT%\ts.local.bat.example" >nul
copy /Y "%cd%\deploy\caddy\CADDY_VERSION.txt" "%TS_OUT%\caddy\CADDY_VERSION.txt" >nul
copy /Y "%cd%\Trader_Server\caddy\caddy.exe" "%TS_OUT%\caddy\caddy.exe" >nul
if errorlevel 1 goto :fail

echo [TS] 打包完成：%cd%\dist\TraderServer\TraderServer\TraderServer.exe
endlocal
exit /b 0

:caddy_missing
echo [TS] 缺少 Trader_Server\caddy\caddy.exe，无法生成生产包。
endlocal
exit /b 1

:fail
echo [TS] 打包失败。
endlocal
exit /b 1
