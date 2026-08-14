@echo off
title HYDRA Launcher
cd /d "%~dp0"

echo ========================================
echo  HYDRA - Starting All Services
echo ========================================
echo.
echo  Agent path = capital-preservation rails (not growth-alpha claim).
echo  Heartbeat P(up) starts from start_hydra.bat (BTC/ETH only; no orders).
echo  S3 shadow: set HYDRA_S3_STRATEGY=1 (still no orders).
echo.

:: Start dashboard in a new window
start "HYDRA Dashboard" cmd /c start_dashboard.bat

:: Small delay to let dashboard bind its port
timeout /t 3 /nobreak >nul

:: Agent watchdog. start_hydra.bat starts heartbeat once before the
:: restart loop (idempotent). Do not also launch start_heartbeat.bat
:: here -- a python -m fallback would not show heartbeat.exe and
:: would spawn a second pair of confirmer windows.
start "HYDRA Agent" cmd /c start_hydra.bat

echo All services launched. Close the windows to stop.
