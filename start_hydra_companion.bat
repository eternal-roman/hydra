@echo off
title HYDRA Trading Agent (Companion Mode)
cd /d "%~dp0"

REM ═══════════════════════════════════════════════════════════════
REM  HYDRA launcher for companion testing.
REM
REM  The companion subsystem is now default-on: chat, proposals, and
REM  proactive nudges are active without any env vars. Clicking the
REM  orb in the dashboard IS the activation \u2014 no setup required.
REM
REM  This launcher runs in --paper mode so no real orders land during
REM  testing. Copy start_hydra.bat for the production incantation.
REM
REM  Opt-outs (only set these if you want to disable something):
REM    HYDRA_COMPANION_DISABLED=1           kill switch (no orb)
REM    HYDRA_COMPANION_PROPOSALS_ENABLED=0  no trade cards
REM    HYDRA_COMPANION_NUDGES=0             no proactive messages
REM
REM  Live execution stays opt-in (money safety):
REM    HYDRA_COMPANION_LIVE_EXECUTION=1     real orders (not set here)
REM ═══════════════════════════════════════════════════════════════

echo ========================================
echo  HYDRA - Companion Mode (Paper)
echo ========================================
echo   Chat + proposals + nudges: ON (default)
echo   Live execution:            OFF
echo   Trade mode:                --paper
echo ========================================
echo.

:loop
echo [%date% %time%] Starting HYDRA agent...
python -u hydra_agent.py --pairs auto --mode competition --paper
echo.
echo [%date% %time%] HYDRA exited (code %errorlevel%). Restarting in 10 seconds...
echo Press Ctrl+C to stop.
timeout /t 10 /nobreak >nul
goto loop
