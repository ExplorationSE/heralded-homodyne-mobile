@echo off
rem Heralded Homodyne Mobile - gemischter Betrieb: HydraHarp live, ADQ32 simuliert. Geraetepruefung, dann Server (config\server_live_nur_harp.toml)
rem Optionen wie start_messung.bat: -Online (Tunnel + QR-Code)  -NoBrowser  -Port 8766
cd /d "%~dp0"
set PYTHONUTF8=1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_messung.ps1" -Config "config\server_live_nur_harp.toml" %*
if errorlevel 1 pause
