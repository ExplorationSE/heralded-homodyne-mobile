@echo off
rem Heralded Homodyne Mobile - online ueber Cloudflare Quick Tunnel starten
cd /d "%~dp0"
rem Rechenzeit (0.15): je Prozess 1 BLAS-Thread, parallel wird ueber Prozesse gerechnet (hhd.parallel)
if not defined OPENBLAS_NUM_THREADS set OPENBLAS_NUM_THREADS=1
if not defined OMP_NUM_THREADS set OMP_NUM_THREADS=1
if not defined MKL_NUM_THREADS set MKL_NUM_THREADS=1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_online.ps1" %*
if errorlevel 1 pause
