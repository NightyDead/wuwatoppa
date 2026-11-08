@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
set "PET_PYTHON=python"
set "PET_PYTHONW=pythonw"
if exist "D:\Anaconda\python.exe" set "PET_PYTHON=D:\Anaconda\python.exe"
if exist "D:\Anaconda\pythonw.exe" set "PET_PYTHONW=D:\Anaconda\pythonw.exe"
if exist "D:\anaconda3\python.exe" set "PET_PYTHON=D:\anaconda3\python.exe"
if exist "D:\anaconda3\pythonw.exe" set "PET_PYTHONW=D:\anaconda3\pythonw.exe"
if /i "%~1"=="--check" (
    "%PET_PYTHON%" "%~dp0程序\launcher.py" --check
    exit /b
)
start "" "%PET_PYTHONW%" "%~dp0程序\launcher.py"
if errorlevel 1 pause
