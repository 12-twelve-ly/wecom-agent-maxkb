@echo off
chcp 65001 >nul
title WeCom ^<^> MaxKB Gateway
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo 未找到 .venv，请先执行：
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -r src\requirements.txt
    pause
    exit /b 1
)

if exist "config\config.toml" (
    ".\.venv\Scripts\python.exe" "src\bot.py" --config "config\config.toml"
) else (
    ".\.venv\Scripts\python.exe" "src\bot.py"
)

pause
