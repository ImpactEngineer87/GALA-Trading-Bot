@echo off
:: ============================================================
:: GALA6 Chocoletra Bot -- SERVICIO con auto-reinicio (B2 del plan)
:: Mantiene server.py vivo: si se cae o el server reinicia, vuelve solo.
:: Se registra como tarea programada al inicio (ver INSTALAR_SERVICIO.bat).
:: ============================================================
title GALA6 Bot -- Servicio
cd /d "%~dp0"
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8

:loop
echo [%date% %time%] Iniciando server.py (dashboard :5000)...
".venv\Scripts\python.exe" server.py
echo [%date% %time%] server.py se detuvo (codigo %errorlevel%). Reinicio en 5s...
timeout /t 5 /nobreak >nul
goto loop
