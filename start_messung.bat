@echo off
rem Heralded Homodyne Mobile (Harp ADQ32 HHD) - Messung mit Hardware: Geraetepruefung, dann Server (config\server_live.toml)
rem Optionen: -Online (Tunnel + QR-Code)  -Trotzdem (gemischter Betrieb)  -NoBrowser  -Port 8766
cd /d "%~dp0"
set PYTHONUTF8=1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_messung.ps1" %*
if errorlevel 1 pause
