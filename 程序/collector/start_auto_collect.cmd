@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
if exist "D:\Anaconda\python.exe" (
    "D:\Anaconda\python.exe" "%~dp0auto_collect.py" %*
) else (
    python "%~dp0auto_collect.py" %*
)
if errorlevel 1 pause
