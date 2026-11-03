@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "GRADIO_ANALYTICS_ENABLED=False"
set "PATH=%~dp0;%~dp0.venv\Scripts;%PATH%"
set "PYTHONPATH=%~dp0"
set "PYTHONSAFEPATH=1"
set "NO_PROXY=localhost,127.0.0.1,::1,%NO_PROXY%"
echo ================================================
echo  RVC WebUI  (Python 3.12 + torch 2.7.1 cu128)
echo  启动后浏览器访问: http://127.0.0.1:7865
echo  关闭此窗口即停止服务
echo ================================================
".venv\Scripts\python.exe" webui.py --port 7865 --pycmd ".venv\Scripts\python.exe"
pause
