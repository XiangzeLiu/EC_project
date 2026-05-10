@echo off
chcp 65001 >nul 2>&1
title Server Manager - 后台管理服务
color 0A
echo ============================================
echo   Server Manager 启动中...
echo   按 Ctrl+C 停止服务
echo ============================================
echo.

:: ── 可在此修改默认配置（或通过环境变量传入）──
set SERVER_HOST=0.0.0.0
set SERVER_PORT=8800
set SERVER_USERNAME=admin
set SERVER_PASSWORD=changeme123

:: 启动
"%~dp0ServerManager.exe"
pause
