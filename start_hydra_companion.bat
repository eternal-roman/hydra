@echo off
title HYDRA Trading Agent (Companion Mode)
cd /d "%~dp0"

REM ═══════════════════════════════════════════════════════════════
REM  HYDRA launcher for companion testing.
REM
REM  Chat and proposals are default-on. Proactive nudges are opt-in in
REM  production (HYDRA_COMPANION_NUDGES=1) and switched on below for this
REM  paper test session. Clicking the orb in the dashboard IS the
REM  activation \u2014 no setup required.
REM
REM  This launcher runs in --paper mode so no real orders land during
REM  testing. Copy start_hydra.bat for the production incantation.
REM
REM  Opt-outs (only set these if you want to disable something):
REM    HYDRA_COMPANION_DISABLED=1           kill switch (no orb)
REM    HYDRA_COMPANION_PROPOSALS_ENABLED=0  no trade cards
REM    HYDRA_COMPANION_NUDGES=0             no proactive messages here
REM
REM  Live execution stays opt-in (money safety):
REM    HYDRA_COMPANION_LIVE_EXECUTION=1     real orders (not set here)
REM ═══════════════════════════════════════════════════════════════

echo ========================================
echo  HYDRA - Companion Mode (Paper)
echo ========================================
REM Nudges default on for this paper session; an operator-set value wins.
if not defined HYDRA_COMPANION_NUDGES set "HYDRA_COMPANION_NUDGES=1"
echo   Chat + proposals:          ON
echo   Nudges:                    HYDRA_COMPANION_NUDGES=%HYDRA_COMPANION_NUDGES%
echo   Live execution:            OFF
echo   Trade mode:                --paper
echo ========================================
echo.

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
python -u hydra_agent.py --pairs auto --mode competition --paper
echo.
echo [%date% %time%] HYDRA exited (code %errorlevel%). Restarting in 10 seconds...
echo Press Ctrl+C to stop.
timeout /t 10 /nobreak >nul
goto loop
