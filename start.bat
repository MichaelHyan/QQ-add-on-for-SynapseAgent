@echo off
chcp 65001 >nul
title QQ-add-on-for-SynapseAgent
cd /d "%~dp0"
python main.py
echo.
echo [已退出] 按任意键关闭...
pause >nul