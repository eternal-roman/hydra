@echo off
title HYDRA Trading Agent
cd /d "%~dp0"

echo ========================================
echo  HYDRA - Auto-Restart Launcher
echo ========================================
echo.

:loop
echo [%date% %time%] Starting HYDRA agent...
python -u hydra_agent.py --pairs SOL/USD,SOL/BTC,BTC/USD --mode competition --resume
echo.
echo [%date% %time%] HYDRA exited (code %errorlevel%). Restarting in 10 seconds...
echo Press Ctrl+C to stop.
timeout /t 10 /nobreak >nul
goto loop
