@echo off
rem APEX ALT Engine — defaults: NIGHT/USD + AAVE/USD + AAVE/BTC
rem Override: start_meme.bat --pairs NIGHT/USD,AAVE/USD --position-size 600 --daily-cap 20
rem Single pair: start_meme.bat --pair AAVE/USD
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

if not exist .env (
    echo ERROR: .env file not found. Set KRAKEN_API_KEY and KRAKEN_API_SECRET.
    pause
    exit /b 1
)

rem Kill stale APEX process if PID file exists
if exist apex_meme.pid (
    set /p OLD_PID=<apex_meme.pid
    echo Stopping previous APEX instance...
    taskkill /PID %OLD_PID% /F >nul 2>&1
    del apex_meme.pid >nul 2>&1
    timeout /t 2 /nobreak >nul
)

rem Kill any orphaned processes holding WS ports 8770-8772
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8770 " ^| findstr "LISTENING" 2^>nul') do (
    echo Killing orphan on port 8770 ^(PID %%a^)...
    taskkill /PID %%a /F >nul 2>&1
)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8771 " ^| findstr "LISTENING" 2^>nul') do (
    echo Killing orphan on port 8771 ^(PID %%a^)...
    taskkill /PID %%a /F >nul 2>&1
)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8772 " ^| findstr "LISTENING" 2^>nul') do (
    echo Killing orphan on port 8772 ^(PID %%a^)...
    taskkill /PID %%a /F >nul 2>&1
)

rem Brief pause for port release
timeout /t 1 /nobreak >nul

rem Launch and record PID via title trick
title APEX_MEME_PID_CAPTURE
python -u hydra_meme_agent.py --pairs NIGHT/USD,AAVE/USD,AAVE/BTC %*

if %errorlevel% neq 0 (
    echo.
    echo APEX exited with code %errorlevel%
    pause
)

rem Clean up PID file on exit
if exist apex_meme.pid del apex_meme.pid >nul 2>&1
