@echo off
chcp 936 >nul
title RVC 一键停止
setlocal
set "BASE=%~dp0"
if "%BASE:~-1%"=="\" set "BASE=%BASE:~0,-1%"
if exist "%BASE%\RVC-Data\runtime\python312\python.exe" set "STACK=%BASE%\RVC-Data"
if not defined STACK set "STACK=%BASE%"

set "PY=%STACK%\RVCSVC\apienv\Scripts\python.exe"
if not exist "%PY%" set "PY=%STACK%\runtime\python312\python.exe"
if not exist "%PY%" goto :nopy
if not exist "%STACK%\RVCSVC\dashboard\stop_service.py" goto :nopy

echo ================================================
echo   RVC 一键停止（连面板一起关）
echo   会结束：后端 3333 / 网易云 3002 / WebUI 7865
echo            公网隧道 cloudflared / 监控面板 8899
echo   只停进程，不动模型、配置和成品库。
echo ================================================
echo.
set /p "Y=确认停止? 直接回车=是，输入 n 取消: "
if /i "%Y%"=="n" goto :bye

set "PYTHONIOENCODING=gbk"
"%PY%" "%STACK%\RVCSVC\dashboard\stop_service.py" --panel
echo.
echo 按任意键关闭窗口。
pause >nul
goto :eof

:nopy
echo [X] 没找到内置 python 或 stop_service.py。
echo     请确认本文件在移动硬盘根目录（和 RVC-Data 同一层）。
echo.
pause >nul
goto :eof

:bye
