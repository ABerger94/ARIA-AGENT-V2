@echo off
rem ARIA v2 launcher - double-click to start. No terminal typing needed.
cd /d %~dp0
echo Installing dependencies (skips anything already installed)...
python -m pip install -r requirements.txt
echo.
echo Starting ARIA v2...
python main.py
echo.
echo ARIA has exited. Press any key to close this window.
pause >nul
