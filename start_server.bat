@echo off
chcp 65001 >nul
echo ========================================
echo  Trading Proxy Server
echo ========================================

:: ── 服务器访问账号（本地插件连服务器用）──
set SERVER_USERNAME=admin
set SERVER_PASSWORD=changeme123

:: ── 券商账号（填入你的 secret 和 token）──
set TASTY_SECRET=填入你的secret
set TASTY_TOKEN=填入你的token

:: ── 服务器端口 ──
set SERVER_PORT=8800

echo Server Username: %SERVER_USERNAME%
echo Server Port:     %SERVER_PORT%
echo.
echo Starting server...

python server.py

pause
