@echo off
setlocal EnableDelayedExpansion
:: ============================================================
:: GALA10 CHOCOLETRA BOT -- EMERGENCY PROCESS STOP
:: Normal shutdown must be performed from the dashboard so the
:: broker kill switch can reconcile and close managed positions.
:: ============================================================

title GALA10 Bot -- Emergency Stop
color 0C
chcp 65001 >nul 2>&1

echo.
echo  WARNING: This is an abrupt emergency process termination.
echo  It cannot confirm broker cancellations or position closure.
echo.
echo  For normal shutdown, use the Dashboard bot toggle.
echo.
set /p CONFIRM=Type EMERGENCY to force-stop the local process: 
if /I not "%CONFIRM%"=="EMERGENCY" (
    echo Cancelled. No process was stopped.
    timeout /t 2 >nul
    exit /b 0
)

taskkill /f /fi "WINDOWTITLE eq GALA10 Dashboard" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq GALA10 Trading Bot" >nul 2>&1

for /f "tokens=2" %%i in ('tasklist /fi "imagename eq python.exe" /fo csv /nh') do (
    wmic process where "ProcessId=%%~i" get CommandLine 2^>nul | findstr /I "server.py main.py" >nul
    if !errorlevel! equ 0 taskkill /pid %%~i /f >nul 2>&1
)

echo.
echo Local process terminated. Verify positions and orders directly in Schwab.
echo.
timeout /t 3 >nul
endlocal
