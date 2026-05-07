@echo off
rem APEX Meme Engine — defaults: PLAY/USD, $300 position, $30 daily cap
rem Override: start_meme.bat --pair WIF/USD --position-size 600 --daily-cap 20
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
if not exist .env (
    echo ERROR: .env file not found. Set KRAKEN_API_KEY and KRAKEN_API_SECRET.
    pause
    exit /b 1
)
python -u hydra_meme_agent.py --pair PLAY/USD %*
if %errorlevel% neq 0 (
    echo.
    echo APEX exited with code %errorlevel%
    pause
)
