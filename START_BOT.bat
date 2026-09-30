@echo off
:: ============================================================
:: GALA10 CHOCOLETRA BOT -- START SCRIPT
:: ============================================================

title GALA10 Bot -- Launcher
color 0A

cd /d "%~dp0"

:: Force UTF-8 in Windows console (prevents emoji crash)
chcp 65001 >nul 2>&1

echo.
echo  ============================================================
echo   GALA10 CHOCOLETRA BOT -- STARTING...
echo  ============================================================
echo.

:: Kill any existing bot windows
taskkill /f /fi "WINDOWTITLE eq GALA10 Dashboard" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq GALA10 Trading Bot" >nul 2>&1
timeout /t 2 /nobreak >nul

:: Start Dashboard ONLY -- server.py manages bot via subprocess
echo [1/1] Starting Dashboard + Bot Server...
start "GALA10 Dashboard" cmd /k ^
    "chcp 65001 >nul && cd /d "%~dp0" && color 0B && echo. && echo Dashboard: http://127.0.0.1:5000 && echo Use credentials from .env && echo. && set PYTHONIOENCODING=utf-8 && python server.py"

timeout /t 4 /nobreak >nul

:: Show access info
echo.
echo  ============================================================
echo   DASHBOARD IS RUNNING!
echo.
echo   URL: http://127.0.0.1:5000
echo   Login credentials are stored in .env and are never printed.
echo.
echo   To START the bot: Go to Dashboard and click the toggle
echo   To STOP the bot:  Click toggle again or close the window
echo  ============================================================
echo.

:: Open dashboard in browser
start http://127.0.0.1:5000

echo Press any key to close this launcher (server keeps running)...
pause >nul
