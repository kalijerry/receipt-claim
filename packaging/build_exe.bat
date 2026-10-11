@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
rem 打包 Windows 版：生成 packaging\dist\报销助手\（可直接运行）和 packaging\receipt-claim-windows-x64-v<版本>.zip
rem 需要联网（安装依赖、下载识别模型），约 5 分钟。
cd /d "%~dp0.." || goto :fail
set "VENV=packaging\.buildvenv"

if exist "%VENV%\Scripts\python.exe" goto :install
set "PY="
for %%V in (3.12 3.13 3.11) do if not defined PY py -%%V -c "pass" >nul 2>nul && set "PY=py -%%V"
if not defined PY python -c "import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)" >nul 2>nul && set "PY=python"
if not defined PY goto :nopython
echo 创建打包环境...
%PY% -m venv "%VENV%" || goto :fail

:install
echo 安装依赖...
"%VENV%\Scripts\python.exe" -m pip install -q -r app\requirements.txt pyinstaller || goto :fail

echo 准备识别模型（首次需要下载）...
pushd app
"..\%VENV%\Scripts\python.exe" -c "import native; print(native.warmup())" || goto :fail
popd

echo 生成第三方组件说明...
"%VENV%\Scripts\python.exe" packaging\make_notices.py || goto :fail

echo 打包...
if exist packaging\dist rmdir /s /q packaging\dist
if exist packaging\build rmdir /s /q packaging\build
"%VENV%\Scripts\python.exe" -m PyInstaller packaging\receipt-claim.spec --noconfirm --distpath packaging\dist --workpath packaging\build || goto :fail

echo 压缩...
"%VENV%\Scripts\python.exe" packaging\make_zip.py || goto :fail
echo.
echo 完成。
exit /b 0

:nopython
echo 没有找到 Python 3.11 - 3.13，请先安装 Python 3.12。
exit /b 1

:fail
echo.
echo 打包失败，请查看上面的提示。
exit /b 1
