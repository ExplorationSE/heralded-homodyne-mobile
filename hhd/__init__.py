"""ExplorationSE Harp ADQ32 HHD Mobil – Kernpaket ``hhd`` (HHD = Heralded Homodyne).

Teilpakete: ``hhd.sim`` (Simulator, AP2), ``hhd.io`` (Dateiformate, Import), ``hhd.sync`` (Synchronisation und
Zuordnung). Die weiteren Teilpakete laut Gesamtplan (calib, modes, recon, compare, devices, run, ...) folgen.
"""

__version__ = "0.15.0"

# BLAS-Threads (ab 0.15): OpenBLAS/MKL starten je Matrix-Vektor-Produkt sonst so viele Threads wie Kerne. Die Kerne der
# Auswertung sind dafür zu klein; unter Last (Aufnahme, Live-Vorschau, parallele Prozesse) wird es 17–22× langsamer
# (Messung 21.10.2026, docs/ANALYSE.md „Rechenzeit“). Gesetzt wird nur, was nicht schon vorgegeben ist, und vor dem
# ersten numpy-Import über hhd (spawn-Kinder unter Windows erben die Umgebung). ``HHD_BLAS_THREADS`` ändert die Zahl.
# Ist numpy schon geladen, begrenzt ``hhd.parallel.limit_blas_threads`` zur Laufzeit (threadpoolctl).
import os as _os

BLAS_ENV = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "BLIS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
            "NUMEXPR_NUM_THREADS")
for _v in BLAS_ENV:
    _os.environ.setdefault(_v, _os.environ.get("HHD_BLAS_THREADS", "1"))
del _v
