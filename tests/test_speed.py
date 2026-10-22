"""Rechenzeit (ab 0.15): SQUAREM-MaxLik, HDF5 Blosc-LZ4/gzip, Bootstrap auf mehreren Prozessen, BLAS-Threads.

* SQUAREM liefert dieselbe Lösung wie die normale RρR-Iteration (innerhalb der Toleranz, gegen tol 1e-14).
* digitizer.h5: Blosc-LZ4 (Filter 32001) schreiben/lesen; gzip-Dateien (bis 0.14) bleiben lesbar.
* Bootstrap/Jackknife: Ergebnis unabhängig von der Worker-Zahl (gleicher Seed → identische Zahlen).
"""
from __future__ import annotations

import json
import os

import h5py
import numpy as np
import pytest

from hhd.io import h5filters
from hhd.recon import core


def _fock_mix_samples(rng, n, p):
    """Phasengemittelte Quadraturwerte einer Fock-Mischung p (Verwerfungsverfahren, Vakuumvarianz 1/2)."""
    out = []
    P = None
    while sum(len(o) for o in out) < n:
        x = rng.normal(0, 2.0, 4 * n)
        P = core.patterns(x, len(p) - 1)
        f = p @ P
        g = np.exp(-x ** 2 / 8) / np.sqrt(8 * np.pi)
        keep = rng.uniform(0, 1, len(x)) < f / (g * 4.0)
        out.append(x[keep])
    return np.concatenate(out)[:n]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_squarem_equals_plain_iteration(seed):
    rng = np.random.default_rng(seed)
    p_true = np.array([0.35, 0.25, 0.2, 0.1, 0.05, 0.03, 0.02])
    x = _fock_mix_samples(rng, 20000, p_true)
    P = core.patterns(x, 10)
    ref = core._iterate(P, None, 2, 400000, 1e-14, None, accel=False)
    for warm in (None, ref[0] * 0.5 + 0.5 / 11):
        a = core._iterate(P, None, 2, 20000, 1e-8, warm, accel=True)
        b = core._iterate(P, None, 2, 20000, 1e-8, warm, accel=False)
        assert a[2]                                          # konvergiert
        assert abs(core.wigner00(a[0]) - core.wigner00(ref[0])) < 1e-5
        assert np.max(np.abs(a[0] - ref[0])) < 1e-4
        assert abs(core.wigner00(a[0]) - core.wigner00(b[0])) < 5e-4   # normale Iteration selbst nur bis ~tol·n_iter
        assert a[3] >= b[3] - 1e-9                           # Likelihood nicht kleiner als bei RρR
        assert a[1] <= b[1]                                  # nicht mehr Schritte


def test_squarem_switch_env(monkeypatch):
    rng = np.random.default_rng(4)
    x = rng.normal(0, np.sqrt(0.5 * 1.6), 5000)              # thermisch, n̄ = 0,3
    P = core.patterns(x, 8)
    monkeypatch.setattr(core, "ACCEL", False)
    p0, n0, ok0, _ = core._iterate(P, None, 2, 20000, 1e-10)
    monkeypatch.setattr(core, "ACCEL", True)
    p1, n1, ok1, _ = core._iterate(P, None, 2, 20000, 1e-10)
    assert ok0 and ok1 and n1 < n0
    assert np.max(np.abs(p0 - p1)) < 1e-5


def test_blosc_roundtrip_session(tmp_path, monkeypatch):
    from conftest import small_standard
    from hhd.io.session import load_session
    from hhd.sim.engine import run
    monkeypatch.setenv("HHD_H5_COMPRESSION", "blosc-lz4")
    d = tmp_path / "blosc"
    run(small_standard(seed=21), d)
    files = sorted(d.rglob("digitizer.h5"))
    assert files
    with h5py.File(files[0], "r") as f:
        ds = f["blocks/heralded/records/data"]
        assert h5filters.BLOSC_ID in h5filters.dataset_filters(ds)
        raw = ds[...]
    monkeypatch.setenv("HHD_H5_COMPRESSION", "none")
    d2 = tmp_path / "plain"
    run(small_standard(seed=21), d2)
    with h5py.File(sorted(d2.rglob("digitizer.h5"))[0], "r") as f:
        assert np.array_equal(f["blocks/heralded/records/data"][...], raw)       # verlustfrei, gleiche Simulation
    s = load_session(d)
    assert sum(r.n_records for r in s.runs) > 0


def test_gzip_legacy_file_readable(tmp_path, monkeypatch):
    """Dateien wie bis 0.14 (gzip-4) bleiben lesbar und auswertbar; gzip ist als Rückfall wählbar."""
    from conftest import small_standard
    from hhd.analysis.pipeline import AnalysisParams, analyze_session
    from hhd.sim.engine import run
    monkeypatch.setenv("HHD_H5_COMPRESSION", "gzip")
    d = tmp_path / "gz"
    run(small_standard(seed=22), d)
    with h5py.File(sorted(d.rglob("digitizer.h5"))[0], "r") as f:
        ds = f["blocks/heralded/records/data"]
        assert ds.compression == "gzip" and ds.compression_opts == 4
    monkeypatch.delenv("HHD_H5_COMPRESSION")
    r = analyze_session(d, AnalysisParams(n_boot=4, jackknife_groups=0, pca=False, workers=1), write=False)
    assert r["performance"]["n_records"] > 0


def test_compression_choice_and_fallback(monkeypatch):
    monkeypatch.delenv("HHD_H5_COMPRESSION", raising=False)
    assert h5filters.requested() == "blosc-lz4"
    h5filters.set_compression("gzip")
    try:
        assert h5filters.kwargs(10) == dict(compression="gzip", compression_opts=4)
    finally:
        h5filters.set_compression(None)
    with pytest.raises(ValueError):
        h5filters.set_compression("zstd")
    monkeypatch.setenv("HHD_H5_COMPRESSION", "none")
    assert h5filters.kwargs(10) == {}


def _strip(r):
    r = dict(r)
    r.pop("performance", None)
    r.pop("params", None)                                    # enthält workers
    return json.loads(json.dumps(r, default=str, sort_keys=True))


def test_bootstrap_reproducible_across_workers(std_run):
    from hhd.analysis.pipeline import AnalysisParams, analyze_session
    from hhd.parallel import shutdown_pool
    _, d = std_run
    ap = dict(n_boot=8, jackknife_groups=4, pca=False, seed=7, parallel_min_values=0)
    try:
        r1 = analyze_session(d, AnalysisParams(**ap, workers=1), write=False)
        r2 = analyze_session(d, AnalysisParams(**ap, workers=2), write=False)
        r3 = analyze_session(d, AnalysisParams(**ap, workers=3), write=False)
    finally:
        shutdown_pool()
    assert r2["performance"]["workers"] == 2
    a, b, c = _strip(r1), _strip(r2), _strip(r3)
    assert a == b == c


def test_workers_resolution(monkeypatch):
    from hhd import parallel
    monkeypatch.delenv("HHD_WORKERS", raising=False)
    assert parallel.resolve_workers(0) == parallel.available_cpus() >= 1
    assert parallel.resolve_workers(1) == 1
    monkeypatch.setenv("HHD_WORKERS", "3")
    assert parallel.resolve_workers(0) == 3
    assert parallel.get_pool(1) is None


def test_blas_env_set_before_numpy():
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if not k.endswith("_NUM_THREADS")}
    out = subprocess.run([sys.executable, "-c", "import hhd, os; print(os.environ['OPENBLAS_NUM_THREADS'],"
                          " os.environ['MKL_NUM_THREADS'])"], capture_output=True, text=True, env=env,
                         cwd=os.path.dirname(os.path.dirname(__file__)))
    assert out.stdout.split() == ["1", "1"]


def _crash_on_3(j):
    import multiprocessing as mp
    if j == 3 and os.environ.get("HHD_TEST_CRASH") == "1" and mp.parent_process() is not None:
        os._exit(1)
    return j * j


def test_pool_failure_falls_back_to_serial(monkeypatch):
    from hhd import parallel
    monkeypatch.setenv("HHD_TEST_CRASH", "1")                 # Kindprozesse erben die Umgebung
    try:
        with pytest.warns(RuntimeWarning):
            out = list(parallel.ordered_map(_crash_on_3, range(8), 2))
    finally:
        parallel.shutdown_pool()
    assert out == [j * j for j in range(8)]
