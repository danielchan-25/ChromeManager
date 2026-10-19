@echo off
setlocal
chcp 65001 >nul
title ChromeManager - http://127.0.0.1:8765
cd /d "%~dp0"
if errorlevel 1 (
    echo ERROR: Cannot access the ChromeManager directory.
    pause
    exit /b 1
)

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo ERROR: Project Python environment is missing.
    echo Please install the project dependencies first.
    pause
    exit /b 1
)

if not defined CHROME_MANAGER_DATA_ROOT (
    if exist "%~dp0data\chrome_manager.db" (
        set "CHROME_MANAGER_DATA_ROOT=%~dp0"
    ) else (
        set "CHROME_MANAGER_DATA_ROOT=%~dp0.data"
    )
)
set "PYTHONUTF8=1"
set "PYTHONFAULTHANDLER=1"
set "PYTHONUNBUFFERED=1"
set "service_attempt=1"
if not exist "%CHROME_MANAGER_DATA_ROOT%\logs" mkdir "%CHROME_MANAGER_DATA_ROOT%\logs"
if not exist "%CHROME_MANAGER_DATA_ROOT%\logs" (
    echo ERROR: Cannot create the log directory.
    pause
    exit /b 1
)
set "startup_log=%CHROME_MANAGER_DATA_ROOT%\logs\startup.log"

:run_service
>>"%startup_log%" echo [%date% %time%] Starting ChromeManager. Attempt %service_attempt% of 3.
echo ChromeManager: http://127.0.0.1:8765
echo Data directory: %CHROME_MANAGER_DATA_ROOT%
echo Keep this window open. Press Ctrl+C to stop the service.
echo.
"%~dp0.venv\Scripts\python.exe" -m chrome_manager.cli web %*
set "service_exit_code=%errorlevel%"
>>"%startup_log%" echo [%date% %time%] ChromeManager exited. Code: %service_exit_code%.
if "%service_exit_code%"=="20" exit /b 0
if "%service_exit_code%"=="10" goto service_stopped
if "%service_exit_code%"=="0" goto service_stopped
if "%service_exit_code%"=="130" goto service_stopped
if "%service_exit_code%"=="-1073741510" goto service_stopped
if "%service_exit_code%"=="3221225786" goto service_stopped
if %service_attempt% GEQ 3 goto service_stopped
echo.
echo ChromeManager exited unexpectedly. Retrying in 10 seconds. Press Ctrl+C to cancel.
timeout /t 10 /nobreak >nul
if errorlevel 1 goto service_stopped
set /a service_attempt+=1 >nul
goto run_service

:service_stopped
echo.
echo ChromeManager stopped. Exit code: %service_exit_code%
echo Startup log: %startup_log%
pause
exit /b %service_exit_code%
