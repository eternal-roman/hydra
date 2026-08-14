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

if not exist "%~dp0heartbeat\data" mkdir "%~dp0heartbeat\data"

start "HYDRA Heartbeat BTC/USD" /D "%~dp0heartbeat" cmd /c heartbeat run --pair BTC/USD --tf 1h
timeout /t 2 /nobreak >nul
start "HYDRA Heartbeat ETH/USD" /D "%~dp0heartbeat" cmd /c heartbeat run --pair ETH/USD --tf 1h

echo Heartbeat windows launched. Close them to stop.
echo.
