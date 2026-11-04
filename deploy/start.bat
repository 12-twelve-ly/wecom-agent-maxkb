@echo off
chcp 65001 >nul
title WeCom <-> MaxKB Gateway
cd /d "%~dp0.."
echo 正在启动企微智能机器人...
echo 看到 "已连接企微" 即可使用。按 Ctrl+C 可停止。
echo.
if exist "config.toml" (
    ".\.venv\Scripts\python.exe" bot.py --config config.toml
) else (
    ".\.venv\Scripts\python.exe" bot.py
)
pause
