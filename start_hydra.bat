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
REM The heartbeat P(up) research process is opt-in: set
REM HYDRA_START_HEARTBEAT=1 to start it once here, before the watchdog loop.
REM It has no order path, and its committed AUCs predate the 2026-10 label
REM leak fix, so it informs nothing a trade depends on until re-run.
REM start_heartbeat.bat is idempotent if heartbeat.exe is already up.
if "%HYDRA_START_HEARTBEAT%"=="1" call "%~dp0start_heartbeat.bat"
:loop
netstat -ano | findstr /R /C:":8765 .*LISTENING" >nul
if not errorlevel 1 (
  echo ERROR: port 8765 is already in use.
  echo A leftover process is answering dashboard login on that port.
  echo Stop it, then rerun this launcher. The dashboard at :3000 talks to :8765.
  pause
  exit /b 1
)
echo [%date% %time%] Starting HYDRA agent...
python -u hydra_agent.py --pairs auto --mode competition --resume
echo.
echo [%date% %time%] HYDRA exited (code %errorlevel%). Restarting in 10 seconds...
echo Press Ctrl+C to stop.
timeout /t 10 /nobreak >nul
goto loop
