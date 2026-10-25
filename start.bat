@echo off
chcp 65001 >nul
title FatFish-Whisper 控制台
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo [X] 还没安装依赖，请先双击 install.bat
    pause
    exit /b 1
)

echo [..] 启动大肥鱼的内心OS引擎 ...
echo [..] 正在打开控制台网页 ...
start "" http://127.0.0.1:8000/admin
"venv\Scripts\python.exe" packages\core-engine\src\main.py

echo.
echo [!!] 引擎已退出
pause
