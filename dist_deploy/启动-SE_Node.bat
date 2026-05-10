@echo off
chcp 65001 >nul 2>&1
title SE Node - 子服务节点
echo ============================================
echo   SE Node (子服务节点) 启动中...
echo ============================================
echo.

:: ── 部署时必须修改以下地址为实际 SM 服务器 IP ──
set SE_MANAGER_URL=http://127.0.0.1:8800
set SE_NODE_NAME=economic-node-01
set SE_REGION=CN
set SE_WS_PORT=8900

"%~dp0SE_Node.exe" ^
    --manager-url %SE_MANAGER_URL% ^
    --node-name %SE_NODE_NAME% ^
    --region %SE_REGION% ^
    --ws-port %SE_WS_PORT%
