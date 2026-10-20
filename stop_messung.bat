@echo off
rem Heralded Homodyne Mobile (Harp ADQ32 HHD) - Messbetrieb beenden (Server und ggf. Tunnel), wie stop_online.bat
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_online.ps1" %*
pause
