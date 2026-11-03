@echo off
chcp 936 >nul
cd /d "%~dp0"
title Local Netease API :3002
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
rem venv 在上一级 apienv，换盘符不用改
set "PYEXE=%~dp0..\apienv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
"%PYEXE%" -u ncm_proxy.py
echo.
echo Stopped. Press any key to close.
pause >nul
