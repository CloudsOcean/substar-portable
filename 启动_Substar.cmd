@echo off
setlocal
chcp 65001 >nul
title Substar 正在启动
cd /d "%~dp0"
set "SUBSTAR_DATA_ROOT=%~dp0data"
set SUBSTAR_EDITION=slim
set "PATH=%~dp0runtime\ffmpeg\bin;%PATH%"
"%~dp0runtime\python\python.exe" "%~dp0launcher.py"
