@echo off
chcp 936 >nul
title ÍøÒ×ÔÆ Cookie ÉèÖÃ
cd /d "%~dp0"
set "PY=..\..\runtime\python312\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" cookie_setup.py
if errorlevel 1 pause
