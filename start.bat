@echo off
REM ===================================================================
REM  Salary & Payment Reconciliation System - start here (Windows)
REM
REM  Double-click this file. It sets everything up the first time,
REM  then opens the application in your browser.
REM ===================================================================
setlocal
cd /d "%~dp0"

echo.
echo   Salary ^& Payment Reconciliation System
echo   =======================================
echo.

REM --- Find Python ---
set PYTHON=
if exist ".venv\Scripts\python.exe" (
    set PYTHON=.venv\Scripts\python.exe
    goto :ready
)

where python >nul 2>nul
if errorlevel 1 (
    echo   Python was not found on this computer.
    echo.
    echo   Please install Python 3.10 or newer from https://www.python.org/downloads/
    echo   During installation, tick "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

echo   Setting up for the first time. This takes a few minutes...
echo.
python -m venv .venv
if errorlevel 1 (
    echo   Could not create the environment.
    pause
    exit /b 1
)
set PYTHON=.venv\Scripts\python.exe
"%PYTHON%" -m pip install --upgrade pip --quiet
"%PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo   Could not install the required components.
    echo   Please check your internet connection and try again.
    pause
    exit /b 1
)

echo.
echo   Preparing the database...
"%PYTHON%" manage.py init
echo.
set /p SEED="  Load demonstration data so you can look around? (y/n): "
if /i "%SEED%"=="y" "%PYTHON%" manage.py seed & "%PYTHON%" manage.py process

:ready
echo.
echo   Starting. Your browser will open shortly.
echo   To stop the application, close this window or press Ctrl+C.
echo.
start "" http://localhost:8000
"%PYTHON%" manage.py serve --host 127.0.0.1 --port 8000
endlocal
