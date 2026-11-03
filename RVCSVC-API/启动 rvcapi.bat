@echo off
chcp 936 >nul
cd /d "%~dp0"
set "PATH=%~dp0;%PATH%"

rem RVC-WebUI pins gradio 3.14, RVCSVC-API needs gradio 5.49.1
rem so this must run on the dedicated apienv virtualenv
rem 路径相对：apienv 在上一级
"%~dp0..\apienv\Scripts\python.exe" -u app_rvc.py

pause
