@echo off
setlocal
chcp 65001 >nul
rem Relaunch the assistant elevated: Windows blocks input from a normal process
rem to an elevated pet (UIPI), so the chest click never lands.
set "PET_DIR=%~dp0"
if exist "%~dp0桌宠助手.exe" set "PET_TARGET=%~dp0桌宠助手.exe"
if not defined PET_TARGET set "PET_TARGET=%~dp0启动.cmd"
powershell -NoProfile -Command "Start-Process -FilePath $env:PET_TARGET -WorkingDirectory $env:PET_DIR -Verb RunAs"
if errorlevel 1 (
    echo 提权启动失败或被取消。
    echo 也可以右键“桌宠助手.exe”（源码版为“启动.cmd”），选择“以管理员身份运行”。
    pause
)
