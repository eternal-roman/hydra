@echo off
title HYDRA Heartbeat
cd /d "%~dp0"

echo ========================================
echo  HYDRA - Heartbeat P(up) confirmer
echo ========================================
echo.
echo  Research / dashboard only. No order path.
echo  Writes heartbeat\data\heartbeat_status_BTC_USD.json
echo  and heartbeat_status_ETH_USD.json
echo  ZEC is flow-FAIL on the real-tape ledger; not started.
echo.

REM Idempotent: start_hydra.bat and start_all.bat both call this.
REM A leftover heartbeat.exe means the confirmer is already writing
REM status files -- do not spawn a second pair of windows.
tasklist /FI "IMAGENAME eq heartbeat.exe" 2>nul | findstr /I /C:"heartbeat.exe" >nul
if not errorlevel 1 (
  echo Heartbeat already running -- skip launch.
  goto :eof
)

set "HB_LAUNCH=heartbeat"
where heartbeat >nul 2>&1
if errorlevel 1 (
  where python >nul 2>&1
  if errorlevel 1 (
    echo ERROR: heartbeat CLI not on PATH and python not found.
    echo Install with:  pip install -e heartbeat
    goto :eof
  )
  set "PYTHONPATH=%~dp0heartbeat\src;%PYTHONPATH%"
  set "HB_LAUNCH=python -m heartbeat.cli"
  echo heartbeat.exe not on PATH -- using python -m heartbeat.cli
)

if not exist "%~dp0heartbeat\data" mkdir "%~dp0heartbeat\data"

start "HYDRA Heartbeat BTC/USD" /D "%~dp0heartbeat" cmd /c %HB_LAUNCH% run --pair BTC/USD --tf 1h
timeout /t 2 /nobreak >nul
start "HYDRA Heartbeat ETH/USD" /D "%~dp0heartbeat" cmd /c %HB_LAUNCH% run --pair ETH/USD --tf 1h

echo Heartbeat windows launched. Close them to stop.
echo.
