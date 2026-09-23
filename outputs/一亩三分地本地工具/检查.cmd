@echo off
setlocal
set "TASK_PYTHON=%~dp0..\..\work\cf-probe-venv\Scripts\python.exe"
set "PYTHONUTF8=1"
"%TASK_PYTHON%" "%~dp0check.py" %*
exit /b %errorlevel%
