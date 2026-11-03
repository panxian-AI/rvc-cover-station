@echo off
chcp 936 >nul
title RVC 一键启动（移动硬盘版）
setlocal
set "BASE=%~dp0"
if "%BASE:~-1%"=="\" set "BASE=%BASE:~0,-1%"
if exist "%BASE%\RVC-Data\runtime\python312\python.exe" set "STACK=%BASE%\RVC-Data"
if not defined STACK set "STACK=%BASE%"

echo ================================================
echo   RVC 翻唱栈（全在移动硬盘上）
echo   根目录: %STACK%
echo ================================================
echo.

if not exist "%STACK%\runtime\python312\python.exe" goto :no_runtime
if not exist "%STACK%\RVCSVC\apienv\Scripts\python.exe" goto :no_venv

echo [1/2] 校正 venv 路径（盘符变了也能对上）...
"%STACK%\runtime\python312\python.exe" "%STACK%\runtime\fix_paths.py"
if errorlevel 1 goto :fix_failed
echo.

echo [2/2] 启动监控面板: http://127.0.0.1:8899
echo   浏览器会自动打开。进页面后点「一键开启服务」拉起全部服务，
echo   收工点「一键退出服务」，再关这个窗口。
echo.
cd /d "%STACK%\RVCSVC\dashboard"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
"%STACK%\RVCSVC\apienv\Scripts\python.exe" -u panel.py
goto :bye

:no_runtime
echo [X] 没找到内置解释器: %STACK%\runtime\python312\python.exe
echo     请把本文件放在移动硬盘根目录（和 RVC-Data 同一层），或放进 RVC-Data 里。
goto :hold

:no_venv
echo [X] 没找到后端 venv: %STACK%\RVCSVC\apienv
goto :hold

:fix_failed
echo [X] 路径校正失败，已停止。
goto :hold

:bye
echo.
echo 面板已退出。按任意键关闭窗口。
:hold
pause >nul
