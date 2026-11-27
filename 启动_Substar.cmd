@echo off
setlocal
chcp 65001 >nul
title Substar 正在启动
cd /d "%~dp0"
if exist "%~dp0Substar.exe" (
    "%~dp0Substar.exe"
    exit /b
)
set "SUBSTAR_DATA_ROOT=%~dp0data"
set SUBSTAR_EDITION=slim
set "PATH=%~dp0runtime\ffmpeg\bin;%PATH%"
set "SUBSTAR_EXPORT_APP=%~dp0runtime\windows-export\SubstarWindowsExport.exe"
if exist "%LOCALAPPDATA%\SubstarWorkbench\WindowsExport\SubstarWindowsExport.exe" set "SUBSTAR_EXPORT_APP=%LOCALAPPDATA%\SubstarWorkbench\WindowsExport\SubstarWindowsExport.exe"
if exist "%SUBSTAR_EXPORT_APP%" start "" /b "%SUBSTAR_EXPORT_APP%" "%~dp0data\.substar-workbench\runtime"
"%~dp0runtime\python\python.exe" "%~dp0launcher.py"
