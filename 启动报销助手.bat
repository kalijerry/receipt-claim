@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
title 报销助手
rem 双击启动报销助手。首次运行会自动创建 Python 环境、安装依赖并下载文字识别模型（需要联网，只此一次）。
cd /d "%~dp0app" || goto :fail

if exist ".venv\Scripts\python.exe" goto :check

set "PY="
for %%V in (3.13 3.12 3.11 3.10) do if not defined PY py -%%V -c "pass" >nul 2>nul && set "PY=py -%%V"
if not defined PY python -c "import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 13) else 1)" >nul 2>nul && set "PY=python"
if not defined PY goto :nopython
echo 首次运行：正在创建 Python 环境...
%PY% -m venv .venv || goto :fail

:check
rem 依赖清单变了（比如换了识别引擎）就重新安装
fc /b requirements.txt ".venv\requirements.installed" >nul 2>nul && goto :models
echo 正在安装依赖，需要联网，约 350 MB，请耐心等待...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || goto :pipfail
copy /y requirements.txt ".venv\requirements.installed" >nul

:models
if exist "models\en_PP-OCRv5_rec_mobile.onnx" if exist "models\ch_PP-OCRv4_rec_mobile.onnx" goto :run
echo 正在下载文字识别模型（约 30 MB，只此一次）...
".venv\Scripts\python.exe" -c "import native; print(native.impl().warmup())" || goto :modelfail

:run
".venv\Scripts\python.exe" server.py %*
if errorlevel 1 goto :fail
exit /b 0

:nopython
echo.
echo 没有找到 Python 3.10 - 3.13。
echo 请从 https://www.python.org/downloads/windows/ 安装 Python 3.12，安装时勾选 "Add python.exe to PATH"，然后重新双击本文件。
pause
exit /b 1

:pipfail
echo.
echo 依赖安装失败，请检查网络后重新双击本文件重试。
del ".venv\requirements.installed" 2>nul
pause
exit /b 1

:modelfail
echo.
echo 识别模型下载失败，请检查网络后重新双击本文件重试。
pause
exit /b 1

:fail
echo.
echo 启动失败，请查看上面的提示。
pause
exit /b 1
