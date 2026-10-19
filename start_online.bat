@echo off
rem ExplorationSE Harp ADQ32 HHD Mobil - online ueber Cloudflare Quick Tunnel starten
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_online.ps1" %*
if errorlevel 1 pause
