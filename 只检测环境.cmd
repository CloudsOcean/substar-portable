@echo off
cd /d "%~dp0"
set "SUBSTAR_DATA_ROOT=%~dp0data"
set "PATH=%~dp0runtime\ffmpeg\bin;%PATH%"
"%~dp0runtime\python\python.exe" -c "from substar_core.environment_doctor import environment_status; import json; print(json.dumps(environment_status(), ensure_ascii=False, indent=2))"
pause
