@echo off
setlocal
cd /d "%~dp0"

set "python_exe=.venv\Scripts\python.exe"
if exist "%python_exe%" (
    "%python_exe%" -c "import chrome_manager.web" >nul 2>&1
    if not errorlevel 1 goto start_web
)

python -c "import chrome_manager.web" >nul 2>&1
if errorlevel 1 (
    echo No usable Python environment found. Install Python 3.12+, then run:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -e .
    pause
    exit /b 1
)
echo Project .venv is missing dependencies; using Python from PATH.
set "python_exe=python"

:start_web
"%python_exe%" -m chrome_manager.cli web %*
set "result=%errorlevel%"
if not "%result%"=="0" (
    echo ChromeManager stopped with error code %result%.
    pause
)
exit /b %result%
