@echo off
rem Heralded Homodyne Mobile - nur Geraetepruefung (HydraHarp 500 und ADQ32), ohne Messung und ohne Server
cd /d "%~dp0"
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m hhd.server.check --config config\server_live.toml --alle %*
echo.
pause
