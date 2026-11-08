@echo off
setlocal
chcp 65001 >nul
if exist "D:\Anaconda\python.exe" (
    "D:\Anaconda\python.exe" "%~dp0macro.py" %*
) else (
    python "%~dp0macro.py" %*
)
echo.
pause
