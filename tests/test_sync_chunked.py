"""Segmentierte Synchronisation sehr langer Einzelläufe (hhd.sync.chunked): gleiche Zuordnung wie der Batch-Lauf
(0 Abweichungen), Δ gleich, Modenorte bis auf ≪ Toleranz gleich, Speicher unabhängig von der Laufdauer."""
from __future__ import annotations

import gc
import tracemalloc
import warnings

import h5py
import numpy as np
import pytest

from hhd.io.session import load_session
from hhd.sim.config import BlockCfg, SimConfig
from hhd.sim.streaming import StreamingSimulator
from hhd.sync import delay as Dl
from hhd.sync.chunked import Reservoir, SegmentedClock
from hhd.sync.clockmap import ClockMap, Piece
from hhd.sync.pipeline import SyncParams, sync_session, use_chunked

ARRAYS = ("seg_class", "seg_herald", "seg_n_secondary", "seg_overlap_prev", "herald_class", "herald_reason",
          "herald_segment", "seg_n_heralds")


def _session(out, T, L=1000, free=True, sid="LONGRUN"):
    cfg = SimConfig.load("config/standard.yaml")
    cfg.digitizer.record_length, cfg.digitizer.pretrigger = L, int(0.4 * L)
    if free:                                     # freie Digitizer-Uhr mit Irrfahrt, Marker am Marker-Eingang
        cfg.clock.shared_10MHz, cfg.clock.eps, cfg.clock.rw_D = False, 2e-6, 1e-17
        cfg.markers.on_timing_channel = False
    pr = [b for b in cfg.blocks if b.kind == "vacuum"][0].periodic_rate
    prb = [b for b in cfg.blocks if b.kind == "free_running"][0].periodic_rate
    cfg.blocks = [BlockCfg(id="kv_00", kind="vacuum", duration_s=2.0, periodic_rate=pr),
                  BlockCfg(id="dark_00", kind="electronic", duration_s=0.5, periodic_rate=pr),
                  BlockCfg(id="a_long", kind="heralded", duration_s=T),
                  BlockCfg(id="b_01", kind="free_running", duration_s=2.0, periodic_rate=prb),
                  BlockCfg(id="kv_end", kind="vacuum", duration_s=2.0, periodic_rate=pr)]
    cfg.session.id = sid
    StreamingSimulator(cfg, slice_s=0.25).run(out)
    return out


@pytest.fixture(scope="module")
def long24(tmp_path_factory):
    return _session(tmp_path_factory.mktemp("chunk") / "s24", 24.0)


def test_chunked_matches_batch(long24, tmp_path):
    P = dict(delay_max_records=100000, n_boot=20)
    a = sync_session(long24, SyncParams(chunked="never", **P), tmp_path / "batch")
    b = sync_session(long24, SyncParams(chunked="always", segment_s=4.0, overlap_s=0.5, **P), tmp_path / "chunk")
    assert b["runs"]["a_long"]["clock"]["model"] == "segmented" and b["runs"]["a_long"]["clock"]["n_segments"] >= 5
    assert b["runs"]["a_long"]["refine"]["applied"] and b["runs"]["a_long"]["refine"]["n_segments"] >= 5
    assert not b["quality"]["warnings"], b["quality"]["warnings"]
    for k in ("segments", "heralds", "reasons"):
        assert a["quality"][k] == b["quality"][k], k
    n_diff = 0
    with h5py.File(tmp_path / "batch" / "sync.h5") as f, h5py.File(tmp_path / "chunk" / "sync.h5") as g:
        for k in f["runs"]:
            F, G = f["runs"][k], g["runs"][k]
            for n in ARRAYS:
                if n in F:
                    x, y = F[n][:], G[n][:]
                    assert len(x) == len(y), (k, n)
                    n_diff += int(np.sum(x != y))
            assert len(F["record_time_session_s"]) == len(G["record_time_session_s"])
        assert n_diff == 0                                                     # 0 Fehlzuordnungen
        # Modenort (abgebildeter Herald + Δ des Laufs): Unterschied ≪ Toleranz (2 ns) und Modenbreite (40 ns)
        ma = f["runs/a_long/herald_D_s"][:] + a["delta"]["per_run"]["a_long"]["delta_s"]
        mb = g["runs/a_long/herald_D_s"][:] + b["delta"]["per_run"]["a_long"]["delta_s"]
        d = ma - mb
        assert np.std(d) < 0.3e-9 and np.max(np.abs(d)) < 1.5e-9, (np.std(d), np.max(np.abs(d)))
    # gleiche Stichprobe → gleiches Δ_T
    assert b["delta"]["n_records"] == a["delta"]["n_records"]
    assert b["delta"]["delta_T_s"] == pytest.approx(a["delta"]["delta_T_s"], abs=1e-13)
    # Auswertung liest die segmentierte Ausgabe wie die Batch-Ausgabe
    from hhd.analysis.syncdata import load_sync
    sd = load_sync(tmp_path / "chunk")
    assert len(sd.runs["a_long"].seg_class) == load_session(long24).runs[2].n_records


def _peak(fn):
    gc.collect()
    tracemalloc.start()
    try:
        out = fn()
        return tracemalloc.get_traced_memory()[1], out
    finally:
        tracemalloc.stop()


def test_chunked_memory_bounded(tmp_path):
    s1, s2 = _session(tmp_path / "t1", 16.0), _session(tmp_path / "t2", 32.0)
    pc = lambda s: _peak(lambda: sync_session(s, SyncParams(chunked="always", segment_s=4.0, overlap_s=0.5,
                                                            chunk_read_records=4096, calibrate_delay=False), s / "c"))
    pb = lambda s: _peak(lambda: sync_session(s, SyncParams(chunked="never", calibrate_delay=False), s / "b"))
    (c1, r1), (c2, r2) = pc(s1), pc(s2)
    (b1, _), (b2, _) = pb(s1), pb(s2)
    n1, n2 = (r["quality"]["segments"]["zugeordnet"] for r in (r1, r2))
    assert n2 > 1.8 * n1
    assert c2 < 1.2 * c1 + 1.5e6, (c1, c2)          # segmentiert: unabhängig von der Laufdauer
    assert b2 > 1.5 * b1 and c2 < 0.6 * b2, (b1, b2, c2)   # Batch wächst mit der Laufdauer
    # mit Δ: Speicher durch delay_max_records begrenzt
    pd = lambda s: _peak(lambda: sync_session(s, SyncParams(chunked="always", segment_s=4.0, overlap_s=0.5,
                                                            chunk_read_records=4096, delay_max_records=2000,
                                                            n_boot=5), s / "d"))
    (d1, q1), (d2, q2) = pd(s1), pd(s2)
    assert q1["delta"]["available"] and q2["delta"]["available"] and q2["delta"]["n_records"] == 2000
    assert d2 < 1.2 * d1 + 2e6, (d1, d2)


def test_use_chunked_and_segmented_clock(long24):
    s = load_session(long24, lazy=True)
    run = s.runs[2]
    assert use_chunked(run, SyncParams(chunked="auto", chunked_min_s=10.0))
    assert not use_chunked(run, SyncParams())                    # Standard: ab 600 s
    assert use_chunked(s.runs[0], SyncParams(chunked="always")) and not use_chunked(run, SyncParams(chunked="never"))
    with pytest.raises(ValueError):
        use_chunked(run, SyncParams(chunked="ja"))
    m1 = ClockMap(1.0, 1.0 + 1e-6, 0.0)
    m2 = ClockMap(1.0 + 2e-9, 1.0 + 1e-6 + 1e-11, 0.0)               # Nachbarsegment: ns-genau gleich
    m2.pieces.append(Piece(np.array([0.0, 1e-10, -1e-10, 0.0]), 9.0, 1.0))
    sc = SegmentedClock([10.0], [m1, m2], 1.0)
    T = np.linspace(0, 20, 20001)
    H = sc.H(T)
    assert np.all(np.diff(H) > 0) and np.max(np.abs(np.diff(H, 2))) < 1e-11       # stetig (kein Sprung), monoton
    assert np.max(np.abs(sc.D(H) - T)) < 1e-12
    assert np.allclose(sc.H(np.array([5.0])), m1.H(np.array([5.0]))) and np.allclose(sc.H(np.array([15.0])), m2.H(np.array([15.0])))
    rt = SegmentedClock.from_dict(sc.to_dict())
    assert np.allclose(rt.H(T), H, atol=1e-15)


def test_reservoir():
    r = Reservoir(100, seed=3)
    for k in range(50):
        r.add("a" if k % 2 else "b", np.arange(k * 40, k * 40 + 40), np.arange(40, dtype=float))
    ia, _ = r.sources("a"); ib, _ = r.sources("b")
    assert len(ia) + len(ib) == 100 and r.seen == 2000 and np.all(np.diff(ia) > 0)
    assert 0.3 < len(ia) / 100 < 0.7 and np.mean(np.concatenate([ia, ib])) == pytest.approx(1000, rel=0.25)
    s = Reservoir(1000)
    s.add("x", np.arange(10), np.zeros(10))
    assert np.array_equal(s.sources("x")[0], np.arange(10))      # weniger als n_max: alle, Reihenfolge erhalten


def test_delay_scan_no_empty_slice_warning():
    """Rasterpunkte außerhalb aller Records (NaN-Spalten) ergeben keine RuntimeWarning (früher delay.py:142)."""
    rng = np.random.default_rng(1)
    grid = np.linspace(-50e-9, 50e-9, 41)
    V = 0.5 + 0.3 * Dl.overlap(grid - 5e-9, 1 / 40e-9) ** 2
    Q = rng.normal(V, 0.05, (400, len(grid)))
    Q[:, :3] = np.nan                                         # ganz außerhalb
    Q[::2, 3] = np.nan
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        m = Dl.col_mean(Q)
        r = Dl.scan(Q, grid, 1 / 40e-9, "double_exponential", n_boot=5)
    assert np.all(np.isnan(m[:3])) and np.isfinite(m[3:]).all()
    assert abs(r["delta"] - 5e-9) < 2e-9
