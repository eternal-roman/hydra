@echo off
title HYDRA Strategy Evidence Gate
cd /d "%~dp0"

echo ========================================
echo  HYDRA - trend sleeve evidence gate
echo ========================================
echo.
echo  Runs the pre-registered gate on THIS machine's history store and
echo  shows which pairs Hydra will trade with the daily trend sleeve.
echo  Criteria: research\data\trend_sleeve_REGISTRATION.md
echo  Result:   research\data\trend_sleeve_gate.json
echo  No orders are placed. Takes a few minutes.
echo  First run: the gate needs 5+ years of daily history. Build the store
echo  once from Kraken's trade archive:
echo    python -m tools.bootstrap_history --zip KRAKEN_ARCHIVE.zip
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo ERROR: python not found on PATH.
  goto :end
)

echo [1/3] Refreshing hydra_history.sqlite through the kraken CLI...
python -m tools.refresh_history
if errorlevel 1 echo   Refresh failed - the gate will use the store as it is.
echo.

echo [2/3] Running the gate: daily arms and engine arms...
python tools\trend_sleeve_gate.py --engine
if errorlevel 1 (
  echo   Gate run failed - no decision changed.
  goto :end
)
echo.

echo [3/3] What Hydra will trade at its next start:
python -m hydra_strategy_gate
echo.
echo  Restart start_hydra.bat to apply. HYDRA_TREND_SLEEVE=1 or 0 overrides.
echo  After the restart, a pair that newly qualifies buys on its first tick
echo  if its score is already 0.6 or more. A pair that stops qualifying keeps
echo  its coins, and the 1h engine manages them.

:end
echo.
pause
