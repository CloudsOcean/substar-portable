@echo off
setlocal
chcp 65001 >nul
title 停止 Substar
cd /d "%~dp0"
set "SUBSTAR_DATA_ROOT=%~dp0data"
"%~dp0runtime\python\python.exe" "%~dp0launcher.py" --stop
if errorlevel 1 pause
