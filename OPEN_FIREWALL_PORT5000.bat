@echo off
:: ============================================================
:: GALA10 — Public port warning
:: ============================================================
:: Right-click this file -> "Run as Administrator"

title GALA10 — Dashboard Network Safety

echo.
echo ============================================================
echo  BLOCKED: this script no longer opens Flask port 5000 publicly.
echo.
echo  Use the dashboard through the VPS/RDP at:
echo      http://127.0.0.1:5000
echo.
echo  For remote Internet access, place the dashboard behind HTTPS
echo  and restrict the firewall to trusted source addresses.
echo ============================================================
echo.
pause
