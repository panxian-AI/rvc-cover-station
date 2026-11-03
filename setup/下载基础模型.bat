@echo off
chcp 936 >nul
title RVC 基础模型下载
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0.."
set "PY=runtime\python312\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "setup\下载基础模型.py" %*
echo.
pause
