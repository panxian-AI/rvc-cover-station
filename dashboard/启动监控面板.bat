@echo off
chcp 936 >nul
cd /d "%~dp0"
title RVC Panel :8899
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
rem 不写死盘符：面板在本目录，venv 在上一级 apienv
set "PYEXE=%~dp0..\apienv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
"%PYEXE%" -u panel.py
echo.
echo Panel stopped. Press any key to close.
pause >nul
