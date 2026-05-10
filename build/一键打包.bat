@echo off
:: =============================================
::   EC_project 一键打包 + 部署包生成
::   用法: 双击运行此文件（需要已安装 Python）
:: =============================================
cd /d "%~dp0.."
echo ============================================
echo   EC_project Windows 打包工具
echo   打包完成后 exe 在 dist/ 目录
echo   部署脚本在 dist_deploy/ 目录
echo ============================================
echo.
python build/build_all.py
echo.
echo --------------------------------------------
if %ERRORLEVEL% EQU 0 (
    echo 打包成功！正在复制到部署目录...
    if not exist "dist_deploy" mkdir dist_deploy
    copy /y "dist\ServerManager.exe"   "dist_deploy\" >nul 2>&1
    copy /y "dist\SE_Node.exe"         "dist_deploy\" >nul 2>&1
    copy /y "dist\TradingTerminal.exe" "dist_deploy\" >nul 2>&1
    echo.
    echo 部署包已就绪: dist_deploy\
    echo   - 双击 [启动-ServerManager.bat]    启动管理服务
    echo   - 双击 [启动-SE_Node.bat]           启动子服务节点
    echo   - 双击 [启动-TradingTerminal.bat]    启动客户端
) else (
    echo 打包失败，请检查上方错误信息
)
echo --------------------------------------------
pause
