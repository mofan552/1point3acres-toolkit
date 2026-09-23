@echo off
setlocal
set "TASK_PYTHON=%~dp0..\..\work\cf-probe-venv\Scripts\python.exe"
set "PYTHONUTF8=1"
if "%~1"=="" (
  "%TASK_PYTHON%" "%~dp0cli.py" status
) else (
  "%TASK_PYTHON%" "%~dp0cli.py" %*
)
exit /b %errorlevel%
