@echo off
setlocal
:: ============================================================
:: GALA10 CHOCOLETRA BOT -- SAFE FIRST-TIME SETUP
:: Does not train legacy models, open firewall ports, or trade.
:: ============================================================

title GALA10 Bot -- Initial Setup
color 0A
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo.
echo  ============================================================
echo   GALA10 CHOCOLETRA BOT -- SAFE INITIAL SETUP
echo  ============================================================
echo.

echo [1/5] Checking Python...
python --version
if errorlevel 1 (
    echo ERROR: Python is not installed or is not on PATH.
    pause
    exit /b 1
)

echo.
echo [2/5] Creating the local virtual environment when needed...
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Could not create .venv.
        pause
        exit /b 1
    )
)
call ".venv\Scripts\activate.bat"

echo.
echo [3/5] Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Could not install dependencies.
    pause
    exit /b 1
)

echo.
echo [4/5] Compiling and running the safety tests...
python -m compileall -q .
if errorlevel 1 (
    echo ERROR: Python compilation failed.
    pause
    exit /b 1
)
python -m unittest discover -s tests -p "test_*.py" -v
if errorlevel 1 (
    echo ERROR: Safety tests failed. Do not start the bot.
    pause
    exit /b 1
)

echo.
echo [5/5] Security notice...
echo No firewall port was opened. The dashboard remains local to the VPS.
echo Legacy ai_filter training was not executed.
echo Use PRE_ENTRENAR.py only after historical-data validation.

echo.
echo  ============================================================
echo   SETUP COMPLETE
echo   Next read-only check:
echo   python main.py --modo broker-check --tickers MSFT
echo  ============================================================
echo.
pause
endlocal
