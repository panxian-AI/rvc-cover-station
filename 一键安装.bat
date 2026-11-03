@echo off
chcp 936 >nul
setlocal
title RVC 翻唱站 一键安装

rem ================================================================
rem  【下载地址配置】不懂代码也能改：只需要改下面几个等号后面的内容
rem
rem  GH_PREFIX = GitHub 线路。留空 = 自动测速选最快的（推荐）。
rem              想强制指定就填加速前缀，例如:
rem              set "GH_PREFIX=https://ghfast.top/"
rem  GH_LINE2~8 = 备选加速线路（自动测速时会全部测一遍）
rem  PY_URL    = Python 安装包下载地址。留空 = 自动从 3 个镜像测速选最快
rem  PIP_INDEX = Python 包国内镜像源（一般不用改）
rem  TORCH_INDEX = 显卡版 torch 下载源（一般不用改）
rem ================================================================
set "REPO=panxian-AI/rvc-cover-station"
rem  RVC_TAG   = 要安装的版本。留空 = 自动装当前最新发布版本（推荐）
rem              也可以填死某个版本，例如: set "RVC_TAG=v1.0.2"
set "RVC_TAG="
set "GH_PREFIX="
set "GH_LINE1="
set "GH_LINE2=https://ghfast.top/"
set "GH_LINE3=https://ghproxy.net/"
set "GH_LINE4=https://gh.llkk.cc/"
set "GH_LINE5=https://gh-proxy.com/"
set "GH_LINE6=https://gh.kkgithub.com/"
set "GH_LINE7=https://ghproxy.cc/"
set "GH_LINE8=https://ghproxy.link/"
set "PY_URL="
set "PY_URL1=https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
set "PY_URL2=https://mirrors.huaweicloud.com/python/3.12.10/python-3.12.10-amd64.exe"
set "PY_URL3=https://registry.npmmirror.com/-/binary/python/3.12.10/python-3.12.10-amd64.exe"
set "PIP_INDEX=https://mirrors.pku.edu.cn/pypi/simple"
set "TORCH_INDEX=https://mirrors.nju.edu.cn/pytorch/whl/cu128"

echo ==================================================
echo    RVC 翻唱站 一键安装
echo    自动完成：测速选线、下载程序、装 Python、装依赖、下模型
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

echo [0/6] 测速选择下载线路（约 10~40 秒，请稍等）...
set "GH_LIST=%GH_LINE1%;%GH_LINE2%;%GH_LINE3%;%GH_LINE4%;%GH_LINE5%;%GH_LINE6%;%GH_LINE7%;%GH_LINE8%"
set "PY_LIST=%PY_URL1%;%PY_URL2%;%PY_URL3%"
set "GH_AUTO="
set "PY_AUTO="
set "RVC_GH_LINES=%GH_LIST%"
set "RVC_PY_LINES=%PY_LIST%"
for /f "tokens=1,2 delims==" %%i in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; $t='https://raw.githubusercontent.com/panxian-AI/rvc-cover-station/main/README.md'; $ls=$env:RVC_GH_LINES.Split(';'); $best=''; $bi=0; $bt=[double]9e9; $i=0; foreach($p in $ls){ $i++; if($p -eq ''){ $n='direct' } else { $n=$p }; try{ $sw=[Diagnostics.Stopwatch]::StartNew(); $r=Invoke-WebRequest -Uri ($p+$t) -UseBasicParsing -TimeoutSec 6 -DisableKeepAlive; $sw.Stop(); $s=[math]::Round($sw.Elapsed.TotalSeconds,2); Write-Host ('  GitHub ['+$i+'] '+$n+'  '+$s+' s  OK'); if($sw.Elapsed.TotalSeconds -lt $bt){ $bt=$sw.Elapsed.TotalSeconds; $best=$p; $bi=$i } }catch{ Write-Host ('  GitHub ['+$i+'] '+$n+'  fail') } }; $py=$env:RVC_PY_LINES.Split(';'); $bp=''; $j=0; $bt2=[double]9e9; foreach($u in $py){ $j++; try{ $sw=[Diagnostics.Stopwatch]::StartNew(); $r=Invoke-WebRequest -Uri $u -Method Head -UseBasicParsing -TimeoutSec 6 -DisableKeepAlive; $sw.Stop(); $s=[math]::Round($sw.Elapsed.TotalSeconds,2); Write-Host ('  Python ['+$j+'] '+$u.Split('/')[2]+'  '+$s+' s  OK'); if($sw.Elapsed.TotalSeconds -lt $bt2){ $bt2=$sw.Elapsed.TotalSeconds; $bp=$u } }catch{ Write-Host ('  Python ['+$j+'] '+$u.Split('/')[2]+'  fail') } }; Write-Output ('BEST='+$best); Write-Output ('BESTI='+$bi); Write-Output ('PYURL='+$bp)"') do (
  if /i "%%i"=="BEST" set "GH_AUTO=%%j"
  if /i "%%i"=="BESTI" set "GH_BI=%%j"
  if /i "%%i"=="PYURL" set "PY_AUTO=%%j"
)
set "RVC_GH_LINES="
set "RVC_PY_LINES="
if not "%PY_AUTO%"=="" set "PY_URL=%PY_AUTO%"
if "%PY_URL%"=="" set "PY_URL=%PY_URL1%"
if not "%GH_PREFIX%"=="" goto :gh_manual
if "%GH_AUTO%"=="" goto :no_line
set "GH_PREFIX=%GH_AUTO%"
echo.
echo   已自动选中最快的 GitHub 线路：编号 %GH_BI%
set "CH="
set /p "CH=   直接回车用它，或输入编号 1-8 换一条线路："
if "%CH%"=="" goto :gh_done
if "%CH%"=="1" set "GH_PREFIX=%GH_LINE1%"
if "%CH%"=="2" set "GH_PREFIX=%GH_LINE2%"
if "%CH%"=="3" set "GH_PREFIX=%GH_LINE3%"
if "%CH%"=="4" set "GH_PREFIX=%GH_LINE4%"
if "%CH%"=="5" set "GH_PREFIX=%GH_LINE5%"
if "%CH%"=="6" set "GH_PREFIX=%GH_LINE6%"
if "%CH%"=="7" set "GH_PREFIX=%GH_LINE7%"
if "%CH%"=="8" set "GH_PREFIX=%GH_LINE8%"
goto :gh_done
:gh_manual
echo   使用手动指定的线路：%GH_PREFIX%
:gh_done
echo.
echo [0/6] 查询当前最新发布版本...
set "TAG=%RVC_TAG%"
if not "%TAG%"=="" goto :tag_ready
set "RVC_GP=%GH_PREFIX%"
for /f "tokens=1,2 delims==" %%i in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; $ls=@('https://api.github.com/repos/%REPO%/releases/latest'); if($env:RVC_GP){ $ls+=$env:RVC_GP+'https://api.github.com/repos/%REPO%/releases/latest' }; $t=''; foreach($u in $ls){ try{ $r=Invoke-RestMethod -Uri $u -Headers @{'User-Agent'='rvc-installer'} -TimeoutSec 6; if($r.tag_name){ $t=$r.tag_name; break } }catch{} }; Write-Output ('TAG='+$t)"') do (
  if /i "%%i"=="TAG" set "TAG=%%j"
)
set "RVC_GP="
:tag_ready
if "%TAG%"=="" goto :use_main
set "VER=%TAG%"
echo   最新发布版本：%TAG%
set "ZIP_URL=%GH_PREFIX%https://codeload.github.com/%REPO%/zip/refs/tags/%TAG%"
goto :zip_ready
:use_main
set "VER=main 分支最新代码"
echo   没取到发布版本号，改用 main 分支最新代码。
set "ZIP_URL=%GH_PREFIX%https://codeload.github.com/%REPO%/zip/refs/heads/main"
:zip_ready

echo [1/6] 下载程序代码（约 3MB）...
curl.exe -L -o "%INSTALL%\_repo.zip" "%ZIP_URL%"
if not errorlevel 1 goto :dl_ok
if "%TAG%"=="" goto :dl_fail
echo   该版本包下载失败，改用 main 分支最新代码重试...
set "ZIP_URL=%GH_PREFIX%https://codeload.github.com/%REPO%/zip/refs/heads/main"
set "VER=main 分支最新代码"
curl.exe -L -o "%INSTALL%\_repo.zip" "%ZIP_URL%"
if errorlevel 1 goto :dl_fail
:dl_ok

echo [2/6] 解压并整理目录...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%INSTALL%\_repo.zip' -DestinationPath '%INSTALL%\_repo' -Force"
if errorlevel 1 goto :dl_fail
set "SRC="
for /d %%d in ("%INSTALL%\_repo\*") do if exist "%%d\RVC-WebUI\webui.py" set "SRC=%%d"
if "%SRC%"=="" goto :dl_fail
md "%STACK%\RVCSVC" 2>nul
move "%SRC%\RVC-WebUI" "%STACK%\RVC-WebUI" >nul
move "%SRC%\dashboard" "%STACK%\RVCSVC\dashboard" >nul
move "%SRC%\RVCSVC-API" "%STACK%\RVCSVC\RVCSVC-API" >nul
move "%SRC%\ncmapi" "%STACK%\RVCSVC\ncmapi" >nul
move "%SRC%\setup" "%STACK%\RVCSVC\setup" >nul
copy /y "%SRC%\启动RVC.bat" "%INSTALL%\" >nul
copy /y "%SRC%\卸载程序.bat" "%INSTALL%\" >nul
copy /y "%SRC%\README.md" "%INSTALL%\" >nul 2>nul
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
set "API_REQ=%STACK%\RVCSVC\RVCSVC-API\requirements-freeze.txt"
if not exist "%API_REQ%" set "API_REQ=%STACK%\RVCSVC\RVCSVC-API\requirements.txt"
"%STACK%\RVCSVC\apienv\Scripts\python.exe" -m pip install -r "%API_REQ%" -i %PIP_INDEX% --no-warn-script-location
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
echo    本次安装版本：%VER%
echo.
echo    以后使用：双击  %INSTALL%\启动RVC.bat
echo    会自动打开监控面板 http://127.0.0.1:8899
echo    在面板首页点「一键开启全部服务」即可开始翻唱。
echo.
echo    想卸载：双击安装目录里的 卸载程序.bat（可选保留个人数据）
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

:no_line
echo.
echo [X] 所有下载线路都连不上 GitHub。请检查网络后重试；如果公司/校园网
echo     有限制，用记事本打开本文件，在顶部 GH_PREFIX= 填你自己可用的
echo     加速前缀后再运行。
goto :hold

:bad_dir
echo [X] 安装位置不对：不要选盘根目录（如 E:\ 或 C:\），选一个空文件夹或让程序用默认位置。
goto :hold

:dl_fail
echo.
echo [X] 下载失败。重跑本安装器会重新测速换线；也可以用记事本打开本文件，
echo     把顶部 GH_PREFIX= 改成其它加速前缀（如 https://ghproxy.net/ ）。
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
