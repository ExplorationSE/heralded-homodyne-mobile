@echo off
rem Heralded Homodyne Mobile - nur Geraetepruefung (HydraHarp 500 und ADQ32), ohne Messung und ohne Server
cd /d "%~dp0"
rem Rechenzeit (0.15): je Prozess 1 BLAS-Thread, parallel wird ueber Prozesse gerechnet (hhd.parallel)
if not defined OPENBLAS_NUM_THREADS set OPENBLAS_NUM_THREADS=1
if not defined OMP_NUM_THREADS set OMP_NUM_THREADS=1
if not defined MKL_NUM_THREADS set MKL_NUM_THREADS=1
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m hhd.server.check --config config\server_live.toml --alle %*
echo.
pause
