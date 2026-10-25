@echo off
chcp 65001 >nul
title FatFish-Whisper 安装程序
cd /d "%~dp0"

echo [..] 检查 Python ...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [X] 未找到 python，请先安装 Python 3.10+ 并勾选 Add to PATH
    echo     下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)
python --version

echo.
echo [..] 正在为 FatFish-Whisper 创建虚拟环境 ...
if not exist "venv\Scripts\python.exe" (
    python -m venv venv
) else (
    echo [OK] 已存在 venv，跳过创建
)

if not exist "venv\Scripts\python.exe" (
    echo [X] 虚拟环境创建失败
    pause
    exit /b 1
)

echo.
echo [..] 升级 pip ...
"venv\Scripts\python.exe" -m pip install --upgrade pip -q

echo.
echo [..] 安装依赖（先试清华镜像，失败自动回落官方 PyPI）...
"venv\Scripts\python.exe" -m pip install -r packages\core-engine\requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if %errorlevel% neq 0 (
    echo.
    echo [!!] 镜像源安装失败，改用官方 PyPI 重试 ...
    "venv\Scripts\python.exe" -m pip install -r packages\core-engine\requirements.txt
)

if %errorlevel% neq 0 (
    echo.
    echo [X] 安装失败，请检查网络或是否安装了 Python 3.10+
    pause
    exit /b 1
)

echo.
if not exist "packages\core-engine\.env" (
    copy "packages\core-engine\.env.example" "packages\core-engine\.env" >nul
    echo [OK] 已生成 packages\core-engine\.env（请填入你的 LLM API Key）
)

echo.
echo [OK] 安装完成！接下来请双击 start.bat 启动。
pause
