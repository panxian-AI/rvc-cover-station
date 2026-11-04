@echo off
chcp 936 >nul
title RVC 一键启动
setlocal
set "BASE=%~dp0"
if "%BASE:~-1%"=="\" set "BASE=%BASE:~0,-1%"
set "LOG=%BASE%\_启动日志.txt"

rem ============================================================
rem  自适应找安装根目录：含 runtime\python312\python.exe 的那一层
rem  不写死「本文件旁边就是 RVC-Data」——装到哪个盘、文件夹叫什么、
rem  有没有 RVC-Data 这一层，全都不假设。
rem ============================================================
set "STACK="
call :try_stack "%BASE%"
call :try_stack "%BASE%\RVC-Data"
call :try_stack "%BASE%\.."
call :try_stack "%BASE%\..\RVC-Data"
if not defined STACK goto :no_runtime

set "PYRT=%STACK%\runtime\python312\python.exe"
set "APIPY=%STACK%\RVCSVC\apienv\Scripts\python.exe"
set "DASH=%STACK%\RVCSVC\dashboard"
set "LAUNCH=%DASH%\launch_silent.py"
set "FIX="
call :try_fix "%STACK%\runtime\fix_paths.py"
call :try_fix "%STACK%\RVCSVC\setup\fix_paths.py"
call :try_fix "%STACK%\setup\fix_paths.py"

>> "%LOG%" echo ------------------------------------------------------------------
>> "%LOG%" echo RVC 启动 %date% %time%
>> "%LOG%" echo BASE  = %BASE%
>> "%LOG%" echo STACK = %STACK%
>> "%LOG%" echo APIPY = %APIPY%
>> "%LOG%" echo LAUNCH= %LAUNCH%

rem ============================================================
rem  默认静默启动
rem  ------------------------------------------------------------
rem  python.exe 是控制台程序，让它在窗口里跑，桌面上就一直挂着一个黑窗口。
rem  这里改成 start 一个 pythonw.exe（GUI 子系统，天生没有控制台），由它
rem  在后台把面板拉起来、再用浏览器打开控制台页面。本窗口 start 完就退，
rem  一闪而过（不到 1 秒）。
rem
rem  要看窗口排查：命令行里加 --console，或者先 set RVC_CONSOLE=1。
rem  找不到 pythonw.exe / launch_silent.py 时也会自动退回可见窗口，
rem  不会出现「双击了什么都没发生」。
rem ============================================================
if /i "%~1"=="--console" goto :visible
if /i "%RVC_CONSOLE%"=="1" goto :visible
if not exist "%LAUNCH%" goto :visible
if not exist "%APIPY%" goto :visible
set "PYW=%STACK%\RVCSVC\apienv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=%STACK%\runtime\python312\pythonw.exe"
if not exist "%PYW%" goto :visible

cd /d "%DASH%"
start "" "%PYW%" "%LAUNCH%" --log "%LOG%"
>> "%LOG%" echo 已交给静默启动器 pythonw，本窗口退出
exit /b

rem ============================================================
rem  以下是可见窗口模式（排障用）：把面板跑在这个窗口里，
rem  关窗口 = 停面板，出错会停在窗口里等你按键。
rem ============================================================
:visible
>> "%LOG%" echo [可见模式] 走控制台窗口

echo ================================================
echo   RVC 翻唱站
echo   根目录: %STACK%
echo ================================================
echo.

if not exist "%APIPY%" goto :no_venv
if not exist "%DASH%\panel.py" goto :no_panel

rem ---- 先确认 Python 真的能跑，再谈别的 ----
rem 文件存在不等于能用：杀毒删剩半截、安装中断、网盘同步损坏，
rem 都会让 Windows 弹「此应用无法在你的电脑上运行」，用户一脸懵。
echo [1/3] 验证内置 Python 可用...
"%PYRT%" -c "print(1)" >nul 2>nul
if errorlevel 1 goto :py_broken
"%APIPY%" -c "print(1)" >nul 2>nul
if errorlevel 1 goto :py_broken

if not defined FIX goto :skip_fix
echo [2/3] 校正 venv 路径（盘符变了也能对上）...
"%PYRT%" "%FIX%"
if errorlevel 1 echo   [!] 路径校正没成功，继续尝试启动
goto :run

:skip_fix
echo [2/3] 没找到 fix_paths.py，跳过路径校正
:run

echo.
echo [3/3] 启动监控面板: http://127.0.0.1:8899
echo   起来后在面板点「一键开启服务」。
echo   收工点「一键退出服务」；关掉本窗口只是退出面板，服务还在跑，
echo   要全停就双击安装目录里的 停止RVC.bat。
echo.
cd /d "%DASH%"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
"%APIPY%" -u panel.py
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" goto :crashed
echo 面板已退出。按任意键关闭窗口。
pause >nul
goto :eof

:crashed
>> "%LOG%" echo RC=%RC%
echo ================================================
echo [X] 面板没能正常启动（退出码 %RC%）
echo ================================================
echo   常见原因：
echo     1. 8899 端口被占：先双击 停止RVC.bat，再重新双击本文件
echo     2. 依赖没装全：重跑一次 一键安装.bat（可断点续装，不会重下已有文件）
echo     3. 路径被挪动过：把整个安装文件夹放回原处，或重跑安装器
echo.
echo   详细记录已写入: %LOG%
echo   按任意键关闭窗口。
pause >nul
goto :eof

:py_broken
>> "%LOG%" echo [X] python.exe 无法执行
echo ================================================
echo [X] 内置 Python 运行不起来
echo ================================================
echo   如果刚才弹出「此应用无法在你的电脑上运行」，通常是：
echo     1. 杀毒软件 / Windows Defender 把文件删了或隔离了
echo     2. 安装没完成（网络中断，下到半个文件）
echo     3. 文件夹被云盘同步损坏
echo.
echo   解决：重跑一次 一键安装.bat
echo   （会重新下载并安装内置 Python，已装好的模型不会重下）
echo   装完记得把安装目录加入杀毒软件的白名单。
echo.
echo   详细记录已写入: %LOG%
echo   按任意键关闭窗口。
pause >nul
goto :eof

:no_runtime
echo ================================================
echo [X] 没找到内置 Python 解释器
echo ================================================
echo   应该在: runtime\python312\python.exe
echo   本文件位置: %BASE%
echo   试过:
echo     %BASE%
echo     %BASE%\RVC-Data
echo     上一级目录及其 RVC-Data
echo.
echo   多半是安装没跑完，或文件夹被挪动了。
echo   解决：双击安装目录里的 一键安装.bat（可断点续装）。
echo.
>> "%LOG%" echo [X] 没找到解释器 BASE=%BASE%
echo 按任意键关闭窗口。
pause >nul
goto :eof

:no_venv
echo ================================================
echo [X] 没找到后端运行环境 (RVCSVC\apienv)
echo ================================================
echo   根目录上称是: %STACK%
echo   重跑一次 一键安装.bat 可以补上这个环境。
echo.
>> "%LOG%" echo [X] 没找到 apienv STACK=%STACK%
echo 按任意键关闭窗口。
pause >nul
goto :eof

:no_panel
echo ================================================
echo [X] 没找到控制台 (RVCSVC\dashboard\panel.py)
echo ================================================
echo   根目录上称是: %STACK%
echo   重跑一次 一键安装.bat 可以补回文件。
echo.
>> "%LOG%" echo [X] 没找到 panel.py STACK=%STACK%
echo 按任意键关闭窗口。
pause >nul
goto :eof

:try_stack
if defined STACK goto :eof
set "CAND=%~f1"
if "%CAND:~-1%"=="\" set "CAND=%CAND:~0,-1%"
if not exist "%CAND%\runtime\python312\python.exe" goto :eof
set "STACK=%CAND%"
goto :eof

:try_fix
if defined FIX goto :eof
if not exist "%~f1" goto :eof
set "FIX=%~f1"
goto :eof
