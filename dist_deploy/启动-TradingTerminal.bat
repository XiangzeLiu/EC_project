@echo off
chcp 65001 >nul 2>&1
title Trading Terminal - 交易终端客户端
echo 正在启动交易终端...

:: ── 部署时修改为实际服务器地址 ──
"%~dp0TradingTerminal.exe"
