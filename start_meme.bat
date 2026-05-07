@echo off
rem APEX Meme Engine — defaults: PLAY/USD, $600 position, $30 daily cap
rem Override: start_meme.bat --pair WIF/USD --position-size 300 --daily-cap 20
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
python hydra_meme_agent.py --pair PLAY/USD %*
