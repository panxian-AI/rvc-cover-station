@echo off
chcp 936 >nul
setlocal
title RVC 翻唱站 卸载程序

set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
if not exist "%HERE%\RVC-Data\RVCSVC\dashboard\panel.py" goto :wrong_place

echo ==================================================
echo    RVC 翻唱站 卸载程序
echo.
echo    将删除整个安装目录（包括下载的模型，几个 GB）：
echo      %HERE%
echo.
echo    注意：此操作不可恢复！
echo ==================================================
echo.
choice /C YN /N /M "   确定要卸载吗？按 Y=卸载 / N=取消："
if errorlevel 2 goto :cancel

echo.
echo   要保留你的个人数据吗？（翻唱成品、访客申请记录、网易云 Cookie）
choice /C YN /N /M "   按 Y=保留（移到旁边的备份文件夹） / N=全部删除："
if errorlevel 2 goto :stop_srv

set "BK=%HERE%_卸载备份"
md "%BK%" 2>nul
move "%HERE%\RVC-Data\RVCSVC\covers" "%BK%\covers" >nul 2>&1
rem guest_db.json 在 dashboard 目录里（guest.py: DB_PATH = HERE\guest_db.json），
rem 以前写的是 RVCSVC\guest_db.json —— 移不动，错误又被 >nul 吞掉，
rem 然后整个安装目录被 rd /s /q，访客申请/白名单永久消失。
move "%HERE%\RVC-Data\RVCSVC\dashboard\guest_db.json" "%BK%\guest_db.json" >nul 2>&1
move "%HERE%\RVC-Data\RVCSVC\dashboard\advanced_settings.json" "%BK%\advanced_settings.json" >nul 2>&1
move "%HERE%\RVC-Data\RVCSVC\dashboard\public_access.json" "%BK%\public_access.json" >nul 2>&1
move "%HERE%\RVC-Data\RVCSVC\covers\index.json" "%BK%\covers_index.json" >nul 2>&1
move "%HERE%\RVC-Data\RVCSVC\ncmapi\ncm_cookie.txt" "%BK%\ncm_cookie.txt" >nul 2>&1
if exist "%BK%\guest_db.json" echo   个人数据已备份到：%BK%
if exist "%BK%\covers" echo   个人数据已备份到：%BK%
if not exist "%BK%\guest_db.json" if not exist "%BK%\covers" echo   （没有找到可备份的个人数据，跳过）

:stop_srv
echo.
echo   停止相关服务（如果正在运行）...
taskkill /f /im cloudflared.exe >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :3333 ^| findstr LISTENING') do taskkill /f /pid %%p >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :7865 ^| findstr LISTENING') do taskkill /f /pid %%p >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :8899 ^| findstr LISTENING') do taskkill /f /pid %%p >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :3002 ^| findstr LISTENING') do taskkill /f /pid %%p >nul 2>&1

echo   正在删除文件（几个 GB，需要一两分钟，本窗口可关）...
set "CLEAN=%TEMP%\rvc_uninstall_%RANDOM%.cmd"
> "%CLEAN%" echo @cd /d %%TEMP%%
>> "%CLEAN%" echo @ping -n 3 127.0.0.1 >nul
>> "%CLEAN%" echo @rd /s /q "%HERE%"
>> "%CLEAN%" echo @del "%%~f0"
start "" /min cmd /c "%CLEAN%"
echo.
echo   卸载完成！
exit

:cancel
echo.
echo   已取消，什么都没动。
pause
exit /b 0

:wrong_place
echo [X] 本程序需要放在「一键安装」装出来的安装目录里
echo     （和 RVC-Data 同一层）才能卸载，当前目录不对。
echo.
pause
exit /b 1
