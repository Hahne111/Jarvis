@echo off
title J.A.R.V.I.S.
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python -m jarvis.main
pause
