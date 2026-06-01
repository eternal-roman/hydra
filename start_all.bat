@echo off
title HYDRA Launcher
cd /d "%~dp0"

echo ========================================
echo  HYDRA - Starting All Services
echo ========================================
echo.

:: Start dashboard in a new window
start "HYDRA Dashboard" cmd /c start_dashboard.bat

:: Small delay to let dashboard bind its port
timeout /t 3 /nobreak >nul

:: Start agent in a new window
start "HYDRA Agent" cmd /c start_hydra.bat

echo All services launched. Close the windows to stop.
