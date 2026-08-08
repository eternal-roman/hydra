@echo off
title HYDRA Trading Agent
cd /d "%~dp0"

echo ========================================
echo  HYDRA - Auto-Restart Launcher
echo ========================================
echo.

REM Pairs: 'auto' seeds the three v2.29 cores BTC/USD, ETH/USD, ZEC/USD and
REM adds one satellite per additional held asset, so held SOL is worked as a
REM normal tradable satellite. This launcher used to hardcode the legacy
REM SOL/USD,SOL/BTC,BTC/USD triangle, which an explicit --pairs kept alive
REM long after v2.29 retired it as the default - 90d real tape found no SOL
REM edge, AUC 0.56 FAIL, and the SOL/BTC bridge only ever drains exit_only.
REM Production was therefore trading a rejected pair set with no ETH or ZEC.
REM --mode competition --resume are load-bearing: do not remove them.
:loop
echo [%date% %time%] Starting HYDRA agent...
python -u hydra_agent.py --pairs auto --mode competition --resume
echo.
echo [%date% %time%] HYDRA exited (code %errorlevel%). Restarting in 10 seconds...
echo Press Ctrl+C to stop.
timeout /t 10 /nobreak >nul
goto loop
