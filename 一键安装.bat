@echo off
chcp 936 >nul
setlocal
title RVC 翻唱站 一键安装（长效版）

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
rem  AI/脚本无人值守：RVC_UNATTENDED=1 RVC_DEFDIR=安装目录 RVC_GPU=gpu|cpu RVC_NO_START=1
set "RVC_STAGE=start"
rem  RVC_TAG   = 要安装的版本（空 = 长效模式：装 main 分支最新代码，推荐）
rem              以后程序更新，重跑本安装器自动拿到新版，无需重新下载安装包
rem              也可以锁定某个旧版本，例如: set "RVC_TAG=v1.0.3"
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
echo    自动完成：选线路、下载安装 Python、装程序装模型。长效版：装一次，
echo    以后程序更新直接重跑本安装器即可升级，无需重新下载安装包。
echo    全程需要联网。NVIDIA 显卡版约需 12GB 磁盘空间，
echo    CPU 版约需 6GB。安装位置不要选 C 盘根目录。
echo ==================================================
echo.

set "INSTALL=%~dp0RVC-Cover-Station"
rem  外部传了 RVC_DEFDIR 就不弹选择窗口（AI / 批处理调用）
if defined RVC_DEFDIR goto :dir_given
set "RVC_DEFDIR=%INSTALL%"
set "RVC_PICK="
echo    正在打开「文件夹选择」窗口，请在窗口里选安装位置
echo    （窗口若被挡住请看任务栏；直接关掉窗口则改为手动输入路径）。
for /f "delims=" %i in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Add-Type -AssemblyName System.Windows.Forms; $f=New-Object System.Windows.Forms.FolderBrowserDialog; $f.Description='选择 RVC 翻唱站的安装位置（建议新建一个空文件夹，别选 C 盘根目录）'; $f.ShowNewFolderButton=$true; if($env:RVC_DEFDIR){ $f.SelectedPath=$env:RVC_DEFDIR }; $top=New-Object System.Windows.Forms.Form -Property @{TopMost=$true}; if($f.ShowDialog($top) -eq [System.Windows.Forms.DialogResult]::OK){ Write-Output $f.SelectedPath }"') do set "RVC_PICK=%%i"
set "RVC_DEFDIR="
if "%RVC_PICK%"=="" goto :dir_manual
echo    已从窗口选择：%RVC_PICK%
set "INSTALL=%RVC_PICK%"
goto :dir_ready
:dir_manual
set /p "INSTALL=安装到哪个文件夹？直接回车=默认（%INSTALL%）："
:dir_given
if not defined RVC_DEFDIR goto :dir_ready
set "INSTALL=%RVC_DEFDIR%"
set "RVC_STAGE=dir"
echo    无人值守模式：安装位置 %INSTALL%
if not exist "%INSTALL%" md "%INSTALL%" 2>nul
:dir_ready
set "INSTALL=%INSTALL:"=%"
rem  路径归一化：E:/x 这种写法、多余的结尾斜杠都理干净（%%~fp 会解析成绝对路径）
for %%p in ("%INSTALL%") do set "INSTALL=%%~fp"
if "%INSTALL:~-1%"=="\" set "INSTALL=%INSTALL:~0,-1%"
if "%INSTALL%"=="" goto :bad_dir
if "%INSTALL:~2%"=="" goto :bad_dir
echo    安装位置：%INSTALL%
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
if /i "%RVC_UNATTENDED%"=="1" goto :gh_done
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
if /i "%RVC_DRYRUN%"=="1" (
  echo.
  echo [dry-run] 安装位置：%INSTALL%
  echo [dry-run] 当前线路前缀："%GH_PREFIX%"
  echo [dry-run] 显卡模式：%RVC_GPU%
  echo [dry-run] 安装目录已就绪，未下载任何文件。
  endlocal
  exit /b 0
)
echo.
echo [0/6] 确定要安装的程序版本...
set "TAG=%RVC_TAG%"
if not "%TAG%"=="" goto :tag_ready
set "VER=main 分支长效最新"
echo    长效模式：直接下载 main 分支最新代码。
echo    以后程序更新，重跑本安装器即可升级，无需重新下载安装包。
set "ZIP_URL=%GH_PREFIX%https://codeload.github.com/%REPO%/zip/refs/heads/main"
goto :zip_ready
:tag_ready
set "VER=%TAG%"
echo    锁定安装版本：%TAG%
set "ZIP_URL=%GH_PREFIX%https://codeload.github.com/%REPO%/zip/refs/tags/%TAG%"
:zip_ready

set "RVC_STAGE=download"
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
copy /y "%SRC%\VERSION" "%STACK%\RVCSVC\" >nul 2>nul
rd /s /q "%INSTALL%\_repo" 2>nul
del /q "%INSTALL%\_repo.zip" 2>nul
if not exist "%STACK%\RVC-WebUI\webui.py" goto :dl_fail

if exist "%PYRT%" goto :py_ok
set "RVC_STAGE=python"
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
if /i "%RVC_GPU%"=="gpu" set "MODE=gpu"
if /i "%RVC_GPU%"=="gpu" goto :mode_done
if /i "%RVC_GPU%"=="cpu" set "MODE=cpu"
if /i "%RVC_GPU%"=="cpu" goto :mode_done
choice /C GC /N /M "   有 NVIDIA 显卡吗？按 G=有 / 按 C=没有或不确定："
if errorlevel 2 goto :mode_cpu
set "MODE=gpu"
goto :mode_done
:mode_cpu
set "MODE=cpu"
:mode_done
echo   已选择：%MODE%

set "RVC_STAGE=deps"
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
set "RVC_STAGE=models"
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
if /i "%RVC_UNATTENDED%"=="1" if not "%RVC_NO_START%"=="0" goto :bye
if /i "%RVC_NO_START%"=="1" goto :bye
set "ANS="
set /p "ANS=现在就启动吗？回车=启动 / 输入 N=退出："
if /i "%ANS%"=="N" goto :bye
start "" "%INSTALL%\启动RVC.bat"
goto :bye

:no_line
rem  AI / 外部脚本已经探到一条能用的线路时，别因为测速全失败就放弃
if defined RVC_GH_PREFIX (
  echo    测速都没通，改用外部指定的线路：%RVC_GH_PREFIX%
  set "GH_PREFIX=%RVC_GH_PREFIX%"
  goto :gh_done
)
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
set "RVC_STAGE=done"
echo AI_RESULT ok dir=%INSTALL%
if defined INSTALL if exist "%STACK%\RVC-WebUI\webui.py" (
  > "%INSTALL%\_install_ok.txt" echo ok
  >> "%INSTALL%\_install_ok.txt" echo Ver=%VER%
  >> "%INSTALL%\_install_ok.txt" echo Dir=%INSTALL%
  >> "%INSTALL%\_install_ok.txt" echo Time=%DATE% %TIME%
  if exist "%INSTALL%\_install_failed.txt" del /q "%INSTALL%\_install_failed.txt" 2>nul
)
endlocal
exit /b 0

:hold
echo.
echo AI_RESULT fail stage=%RVC_STAGE%
if defined INSTALL if not exist "%INSTALL%" md "%INSTALL%" 2>nul
if defined INSTALL > "%INSTALL%\_install_failed.txt" echo Stage=%RVC_STAGE%
if defined INSTALL >> "%INSTALL%\_install_failed.txt" echo Time=%DATE% %TIME%
if defined INSTALL >> "%INSTALL%\_install_failed.txt" echo Dir=%INSTALL%
if /i "%RVC_UNATTENDED%"=="1" (
  endlocal
  exit /b 1
)
pause
endlocal
