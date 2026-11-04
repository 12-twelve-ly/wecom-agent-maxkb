@echo off
title MaxKB Usage Export
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Run start.bat once to set it up.
    pause
    exit /b 1
)

".\.venv\Scripts\python.exe" "src\usage_report.py" --detail --all --out "logs\usage_records.csv"
if errorlevel 1 (
    echo [ERROR] Export failed. Is the maxkb docker container running?
) else (
    echo Done: logs\usage_records.csv
)

pause
