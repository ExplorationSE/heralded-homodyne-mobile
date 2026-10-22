"""Mehrkern-Betrieb der Auswertung (ab 0.15): BLAS-Threads, Prozess-Pool für Bootstrap/Jackknife.

* **BLAS:** je Prozess 1 Thread (``hhd/__init__.py`` setzt die Umgebung vor dem numpy-Import; hier zusätzlich zur
  Laufzeit über threadpoolctl, falls numpy schon vorher geladen war, z. B. unter pytest).
* **Pool:** ``ProcessPoolExecutor`` mit Startmethode *spawn* (unter Windows die einzige; auf Linux ebenso, damit
  sich beide Systeme gleich verhalten). Die Worker-Funktionen liegen in importierbaren Modulen (nicht ``__main__``),
  der Initialisierer setzt 1 BLAS-Thread und registriert das HDF5-Kompressionsfilter. Der Pool bleibt je Prozess
  bestehen (Start ≈ 0,7 s), wird beim Prozessende geschlossen.
* **Kernzahl:** ``workers`` ≤ 0 bzw. ``"auto"`` → verfügbare Kerne (``os.sched_getaffinity`` bzw.
  ``os.process_cpu_count``/``os.cpu_count``), so dass ein Rechner mit mehr Kernen von selbst schneller rechnet.
  Umgebung ``HHD_WORKERS`` (Zahl) überschreibt die Konfiguration; 1 = seriell (kein Pool).
"""
from __future__ import annotations

import atexit
import os
import threading

from . import BLAS_ENV

_POOL = None
_POOL_N = 0
_LOCK = threading.Lock()


def available_cpus() -> int:
    """Für diesen Prozess nutzbare Kerne."""
    if hasattr(os, "sched_getaffinity"):
        try:
            return max(1, len(os.sched_getaffinity(0)))
        except OSError:
            pass
    f = getattr(os, "process_cpu_count", None) or os.cpu_count
    return max(1, int(f() or 1))


def resolve_workers(workers=0) -> int:
    """Zahl der Worker: Umgebung ``HHD_WORKERS`` > Konfiguration; ≤ 0 / "auto" / None → alle Kerne."""
    env = os.environ.get("HHD_WORKERS", "").strip()
    w = env if env else workers
    if w in (None, "", "auto"):
        w = 0
    w = int(w)
    return available_cpus() if w <= 0 else w


def limit_blas_threads(n: int | None = None) -> bool:
    """BLAS-Threads dieses Prozesses begrenzen (Standard ``HHD_BLAS_THREADS`` bzw. 1). True, wenn threadpoolctl wirkte."""
    n = int(n if n is not None else os.environ.get("HHD_BLAS_THREADS", "1"))
    for v in BLAS_ENV:
        os.environ.setdefault(v, str(n))
    try:
        from threadpoolctl import threadpool_limits
    except ImportError:                                  # ohne threadpoolctl gilt nur die Umgebung (vor dem numpy-Import)
        return False
    threadpool_limits(limits=n)
    return True


def worker_init() -> None:
    """Initialisierer der Pool-Worker: 1 BLAS-Thread, HDF5-Filter registrieren."""
    for v in BLAS_ENV:
        os.environ[v] = "1"
    limit_blas_threads(1)
    from .io import h5filters
    h5filters.register()


def get_pool(workers: int):
    """Gemeinsamer Prozess-Pool mit ``workers`` Prozessen (spawn) oder None für seriell."""
    global _POOL, _POOL_N
    if workers <= 1:
        return None
    with _LOCK:
        if _POOL is not None and _POOL_N == workers:
            return _POOL
        if _POOL is not None:
            _POOL.shutdown(wait=False, cancel_futures=True)
        import concurrent.futures as cf
        import multiprocessing as mp
        _POOL = cf.ProcessPoolExecutor(workers, mp_context=mp.get_context("spawn"), initializer=worker_init)
        _POOL_N = workers
        return _POOL


def shutdown_pool() -> None:
    global _POOL, _POOL_N
    with _LOCK:
        if _POOL is not None:
            _POOL.shutdown(wait=True, cancel_futures=True)
        _POOL, _POOL_N = None, 0


def pool_running() -> bool:
    return _POOL is not None


atexit.register(shutdown_pool)


def ordered_map(fn, jobs, workers: int, chunksize: int = 1):
    """``map`` in Eingabereihenfolge – seriell bei workers ≤ 1, sonst über den gemeinsamen Pool. ``jobs`` darf ein
    Generator sein; es sind höchstens ``4·workers`` Aufträge gleichzeitig unterwegs (begrenzter Speicher)."""
    pool = get_pool(workers)
    if pool is None:
        for j in jobs:
            yield fn(j)
        return
    from collections import deque
    pending = deque()
    it = iter(jobs)
    limit = max(2, 4 * workers)
    try:
        while True:
            while len(pending) < limit:
                try:
                    pending.append(pool.submit(fn, next(it)))
                except StopIteration:
                    break
            if not pending:
                return
            yield pending.popleft().result()
    finally:
        for f in pending:
            f.cancel()
