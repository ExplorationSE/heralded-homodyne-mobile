"""Δ-Kalibrierung mit Summen je Rasterpunkt (0.13): gleiches Δ_T wie die Matrixvariante, Ergebnis unabhängig von
der Blockung, Speicher unabhängig von der Kandidatenzahl."""
import tracemalloc
import warnings

import numpy as np
import pytest

from hhd.sync import delay as Dl

FS = 1e9
GAMMA = 1 / 40e-9
L = 2200
POS0 = 800.0
DT_TRUE = 83.0e-9


def _source(n, seed=5):
    kern = Dl.mode_kernel("double_exponential", GAMMA, FS)
    h = (len(kern) - 1) // 2
    centre = int(round(POS0 + DT_TRUE * FS))

    def read_block(idx):
        out = np.empty((len(idx), L), np.int16)
        for j, i in enumerate(idx):
            r = np.random.default_rng((seed, int(i)))
            y = r.normal(0, np.sqrt(0.5) * 40, L)
            y[centre - h:centre + h + 1] += r.normal(0, 0.8 * 40) * kern
            out[j] = np.round(y)
        return out
    return kern, read_block, np.arange(n), np.full(n, POS0)


def _cal(n, method, chunk=None, n_boot=30, seed=1):
    kern, rb, idx, p0 = _source(n)
    norm = Dl.VacuumNorm(0.0, 40.0, 1, "test")
    return Dl.calibrate([(rb, idx, p0)], FS, kern, norm, GAMMA, "double_exponential", n_boot=n_boot,
                        seed=seed, method=method, chunk=chunk)


def test_stream_matches_matrix():
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        s = _cal(1500, "stream"); m = _cal(1500, "matrix")
    assert s["method"] == "stream" and s["bootstrap"] == "poisson" and m["bootstrap"] == "multinomial"
    assert np.allclose(s["V"], m["V"], rtol=1e-12, atol=0)
    assert np.allclose(s["coarse"]["V"], m["coarse"]["V"], rtol=1e-12, equal_nan=True)
    assert s["delta"] == pytest.approx(m["delta"], abs=1e-15)
    assert abs(s["delta"] - DT_TRUE) < 4e-9
    assert s["n_records"] == m["n_records"] == 1500
    # Bootstrapbreiten statistisch verträglich (n_boot = 30: Schätzfehler ~ 13 %)
    assert 0.5 < s["sigma_boot"] / m["sigma_boot"] < 2.0


def test_stream_independent_of_chunking():
    a = _cal(700, "stream", chunk=37); b = _cal(700, "stream", chunk=256); c = _cal(700, "stream")
    assert np.allclose(a["V"], b["V"], rtol=1e-12) and np.allclose(a["V"], c["V"], rtol=1e-12)
    assert a["sigma_boot"] == pytest.approx(b["sigma_boot"], rel=1e-9)
    assert a["sigma_boot"] == pytest.approx(c["sigma_boot"], rel=1e-9)


def test_grid_accumulator_nan_and_boot():
    acc = Dl.GridAccumulator(4, n_boot=3, seed=2)
    q = np.array([[1.0, np.nan, 2.0, np.nan], [3.0, np.nan, 4.0, 5.0]])
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        acc.add(q[:1]); acc.add(q[1:])
        m = acc.mean(); b = acc.boot_means()
    assert np.allclose(m[[0, 2, 3]], [2.0, 3.0, 5.0]) and np.isnan(m[1])
    assert b.shape == (3, 4) and np.all(np.isnan(b[:, 1])) and acc.n == 2


def _peak(n, method):
    tracemalloc.start()
    r = _cal(n, method, chunk=32, n_boot=20)      # kleine Blöcke: FFT-Arbeitsspeicher klein
    pk = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return pk / 1e6, r


def test_stream_memory_independent_of_candidates():
    s1, r1 = _peak(1000, "stream"); s2, r2 = _peak(5000, "stream")
    m1, _ = _peak(1000, "matrix"); m2, _ = _peak(5000, "matrix")
    assert r2["n_records"] == 5000
    assert s2 < 1.15 * s1 + 1.0, (s1, s2)          # flach
    assert m2 > 2.5 * m1, (m1, m2)                   # Matrix wächst linear
    assert s2 < 0.5 * m2, (s2, m2)
