@echo off
chcp 936 >nul
setlocal
title RVC 翻唱站 一键安装

rem ================================================================
rem  【下载地址配置】不懂代码也能改：只需要改下面几个等号后面的内容
rem
rem  GH_PREFIX = GitHub 加速前缀。国内网络下载失败/很慢时才需要填，
rem              例如  set "GH_PREFIX=https://ghfast.top/"
rem              留空表示直连官方 GitHub。
rem  PY_URL    = Python 安装包下载地址（官方源，一般不用改）
rem  PIP_INDEX = Python 包国内镜像源（一般不用改）
rem  TORCH_INDEX = 显卡版 torch 下载源（一般不用改）
rem ================================================================
set "REPO=panxian-AI/rvc-cover-station"
set "GH_PREFIX="
set "PY_URL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
set "PIP_INDEX=https://mirrors.pku.edu.cn/pypi/simple"
set "TORCH_INDEX=https://mirrors.nju.edu.cn/pytorch/whl/cu128"

set "ZIP_URL=%GH_PREFIX%https://codeload.github.com/%REPO%/zip/refs/heads/main"

echo ==================================================
echo    RVC 翻唱站 一键安装
echo    自动完成：下载程序、安装 Python、装依赖、下模型
echo    全程需要联网。NVIDIA 显卡版约需 12GB 磁盘空间，
echo    CPU 版约需 6GB。安装位置不要选 C 盘根目录。
echo ==================================================
echo.

set "INSTALL=%~dp0RVC-Cover-Station"
set /p "INSTALL=安装到哪个文件夹？直接回车=默认（%INSTALL%）："
set "INSTALL=%INSTALL:"=%"
if "%INSTALL:~-1%"=="\" set "INSTALL=%INSTALL:~0,-1%"
if "%INSTALL%"=="" goto :bad_dir
if "%INSTALL:~2%"=="" goto :bad_dir
echo   安装位置：%INSTALL%
echo.

set "STACK=%INSTALL%\RVC-Data"
set "PYRT=%STACK%\runtime\python312\python.exe"

rem 已完整装过的（重跑本安装器）只补模型，不再重装
if exist "%STACK%\RVC-WebUI\.venv\Scripts\python.exe" if exist "%STACK%\RVCSVC\apienv\Scripts\python.exe" goto :models

echo [1/6] 下载程序代码（约 3MB）...
curl.exe -L -o "%INSTALL%\_repo.zip" "%ZIP_URL%"
if errorlevel 1 goto :dl_fail

echo [2/6] 解压并整理目录...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%INSTALL%\_repo.zip' -DestinationPath '%INSTALL%\_repo' -Force"
if errorlevel 1 goto :dl_fail
if not exist "%INSTALL%\_repo\rvc-cover-station-main\RVC-WebUI" goto :dl_fail
md "%STACK%\RVCSVC" 2>nul
move "%INSTALL%\_repo\rvc-cover-station-main\RVC-WebUI" "%STACK%\RVC-WebUI" >nul
move "%INSTALL%\_repo\rvc-cover-station-main\dashboard" "%STACK%\RVCSVC\dashboard" >nul
move "%INSTALL%\_repo\rvc-cover-station-main\RVCSVC-API" "%STACK%\RVCSVC\RVCSVC-API" >nul
move "%INSTALL%\_repo\rvc-cover-station-main\ncmapi" "%STACK%\RVCSVC\ncmapi" >nul
move "%INSTALL%\_repo\rvc-cover-station-main\setup" "%STACK%\RVCSVC\setup" >nul
copy /y "%INSTALL%\_repo\rvc-cover-station-main\启动RVC.bat" "%INSTALL%\" >nul
copy /y "%INSTALL%\_repo\rvc-cover-station-main\README.md" "%INSTALL%\" >nul 2>nul
rd /s /q "%INSTALL%\_repo" 2>nul
del /q "%INSTALL%\_repo.zip" 2>nul
if not exist "%STACK%\RVC-WebUI\webui.py" goto :dl_fail

if exist "%PYRT%" goto :py_ok
echo [3/6] 下载并安装 Python 3.12.10（装在安装目录内，不改系统设置）...
curl.exe -L -o "%INSTALL%\_py.exe" "%PY_URL%"
if errorlevel 1 goto :dl_fail
"%INSTALL%\_py.exe" /quiet InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_test=0 AssociateFiles=0 Shortcuts=0 TargetDir="%STACK%\runtime\python312"
if errorlevel 1 goto :py_fail
del /q "%INSTALL%\_py.exe" 2>nul
if not exist "%PYRT%" goto :py_fail

:py_ok
echo [4/6] 选择显卡模式...
echo   N 卡（NVIDIA 显卡）速度快；没有或不确定就选 C，能用但慢一些。
choice /C GC /N /M "   有 NVIDIA 显卡吗？按 G=有 / 按 C=没有或不确定："
if errorlevel 2 goto :mode_cpu
set "MODE=gpu"
goto :mode_done
:mode_cpu
set "MODE=cpu"
:mode_done
echo   已选择：%MODE%

echo [5/6] 安装运行依赖（体积大，十几分钟到半小时都正常，别关窗口）...
"%PYRT%" -m venv "%STACK%\RVCSVC\apienv"
if errorlevel 1 goto :dep_fail
"%PYRT%" -m venv "%STACK%\RVC-WebUI\.venv"
if errorlevel 1 goto :dep_fail
if "%MODE%"=="gpu" goto :dep_gpu
"%STACK%\RVC-WebUI\.venv\Scripts\python.exe" -m pip install -r "%STACK%\RVC-WebUI\requirments_cpu_py312.txt" -i %PIP_INDEX% --no-warn-script-location
if errorlevel 1 goto :dep_fail
goto :dep_api
:dep_gpu
"%STACK%\RVCSVC\apienv\Scripts\python.exe" -m pip install torch==2.7.1+cu128 --index-url %TORCH_INDEX% --extra-index-url %PIP_INDEX% --no-warn-script-location
if errorlevel 1 goto :dep_fail
"%STACK%\RVC-WebUI\.venv\Scripts\python.exe" -m pip install torch==2.7.1+cu128 torchaudio==2.7.1+cu128 --index-url %TORCH_INDEX% --extra-index-url %PIP_INDEX% --no-warn-script-location
if errorlevel 1 goto :dep_fail
"%STACK%\RVC-WebUI\.venv\Scripts\python.exe" -m pip install -r "%STACK%\RVC-WebUI\requirments_cu128_py312.txt" -i %PIP_INDEX% --no-warn-script-location
if errorlevel 1 goto :dep_fail
:dep_api
"%STACK%\RVCSVC\apienv\Scripts\python.exe" -m pip install -r "%STACK%\RVCSVC\RVCSVC-API\requirements-freeze.txt" -i %PIP_INDEX% --no-warn-script-location
if errorlevel 1 goto :dep_fail

:models
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "RVC_GH_PREFIX=%GH_PREFIX%"
echo [6/6] 下载基础模型与工具（rmvpe + 小肥鱼声线 + ffmpeg，约 270MB）...
"%PYRT%" "%STACK%\RVCSVC\setup\下载基础模型.py"
if errorlevel 1 echo   （模型下载有失败也不影响安装完成，可重跑本安装器只补模型）
copy /y "%STACK%\RVCSVC\setup\fix_paths.py" "%STACK%\runtime\fix_paths.py" >nul 2>nul

echo.
echo ==================================================
echo    安装完成！
echo.
echo    以后使用：双击  %INSTALL%\启动RVC.bat
echo    会自动打开监控面板 http://127.0.0.1:8899
echo    在面板首页点「一键开启全部服务」即可开始翻唱。
echo.
echo    可选进阶：
echo    · 网易云歌曲：双击 %STACK%\RVCSVC\ncmapi\设置网易云Cookie.bat
echo    · 开放给朋友用（公网）：见 docs\使用说明.md
echo    · hubert 等个别大模型需手动补一次（脚本上方有指引），
echo      没有它生成会报错，获取一次后永久可用。
echo ==================================================
echo.
set "ANS="
set /p "ANS=现在就启动吗？回车=启动 / 输入 N=退出："
if /i "%ANS%"=="N" goto :bye
start "" "%INSTALL%\启动RVC.bat"
goto :bye

:bad_dir
echo [X] 安装位置不对：不要选盘根目录（如 E:\ 或 C:\），选一个空文件夹或让程序用默认位置。
goto :hold

:dl_fail
echo.
echo [X] 下载失败。国内网络请用记事本打开本文件，把最上面 GH_PREFIX= 改成
echo     set "GH_PREFIX=https://ghfast.top/"
echo     保存后重新双击运行。
goto :hold

:py_fail
echo [X] Python 安装失败。可手动下载 %PY_URL%
echo     安装时目标目录填 %STACK%\runtime\python312
goto :hold

:dep_fail
echo [X] 依赖安装失败（多半是网络中断）。直接重新双击本文件即可断点续装。
goto :hold

:bye
endlocal
exit /b 0

:hold
echo.
pause
endlocal
