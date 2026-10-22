@echo off
rem Heralded Homodyne Mobile - gemischter Betrieb: ADQ32 live, HydraHarp simuliert. Geraetepruefung, dann Server (config\server_live_nur_adq32.toml)
rem Optionen wie start_messung.bat: -Online (Tunnel + QR-Code)  -NoBrowser  -Port 8766
cd /d "%~dp0"
rem Rechenzeit (0.15): je Prozess 1 BLAS-Thread, parallel wird ueber Prozesse gerechnet (hhd.parallel)
if not defined OPENBLAS_NUM_THREADS set OPENBLAS_NUM_THREADS=1
if not defined OMP_NUM_THREADS set OMP_NUM_THREADS=1
if not defined MKL_NUM_THREADS set MKL_NUM_THREADS=1
set PYTHONUTF8=1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_messung.ps1" -Config "config\server_live_nur_adq32.toml" %*
if errorlevel 1 pause
