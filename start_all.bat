@echo off
title HYDRA Launcher
cd /d "%~dp0"

echo ========================================
echo  HYDRA - Starting All Services
echo ========================================
echo.
echo  Agent path = capital-preservation rails (not growth-alpha claim).
echo  Heartbeat P(up) starts via start_heartbeat.bat (BTC/ETH only; no orders).
echo  S3 shadow: set HYDRA_S3_STRATEGY=1 (still no orders).
echo.

:: Start dashboard in a new window
start "HYDRA Dashboard" cmd /c start_dashboard.bat

:: Heartbeat confirmer (status files for RESEARCH HB). Independent of the agent.
start "HYDRA Heartbeat" cmd /c start_heartbeat.bat

:: Small delay to let dashboard bind its port
timeout /t 3 /nobreak >nul

:: Start agent in a new window
start "HYDRA Agent" cmd /c start_hydra.bat

echo All services launched. Close the windows to stop.
