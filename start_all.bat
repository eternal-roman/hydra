@echo off
title HYDRA Launcher
cd /d "%~dp0"

echo ========================================
echo  HYDRA - Starting All Services
echo ========================================
echo.

:: Kick the CBP memory sidecar first (idempotent; no-op if already up).
if not defined CBP_RUNNER_DIR set "CBP_RUNNER_DIR=%~dp0..\cbp-runner"
if exist "%CBP_RUNNER_DIR%\supervisor.py" (
    echo [%date% %time%] Bringing up CBP sidecar
    start "CBP Runner Sidecar" cmd /c "cd /d %CBP_RUNNER_DIR% && python supervisor.py"
)

:: Start dashboard in a new window
start "HYDRA Dashboard" cmd /c start_dashboard.bat

:: Small delay to let dashboard bind its port
timeout /t 3 /nobreak >nul

:: Start agent in a new window
start "HYDRA Agent" cmd /c start_hydra.bat

echo Both services launched. Close the windows to stop.
