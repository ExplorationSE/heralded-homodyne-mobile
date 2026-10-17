"""Datenstrom-Simulator (hhd.sim.streaming): konstanter Speicher, gültige Sitzung, Drift weiterhin erkennbar."""
from __future__ import annotations

import gc
import tracemalloc

import numpy as np
import pytest

from hhd.analysis.driftmon import DriftMonitor
from hhd.io.schema import validate_digitizer_file
from hhd.io.session import load_session
from hhd.sim.config import BlockCfg, SimConfig
from hhd.sim.streaming import LazyWalk, StreamingSimulator, TruthDecimator


def _cfg(blocks, L=1000, pre=400, lo_total=0.0, eta_total=0.0, sid="STREAM"):
    cfg = SimConfig()
    cfg.digitizer.record_length, cfg.digitizer.pretrigger = L, pre
    cfg.blocks = blocks
    tot = sum(b.duration_s + cfg.session.gap_s for b in blocks)
    cfg.drift.lo_rel_per_s = lo_total / tot
    cfg.drift.eta_rel_per_s = eta_total / tot
    cfg.session.id = sid
    return cfg


def _peak(cfg, out, slice_s):
    gc.collect()
    tracemalloc.start()
    sim = StreamingSimulator(cfg, slice_s=slice_s)
    sim.run(out)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return peak, sim.n_records


def test_streaming_memory_bounded(tmp_path):
    """Doppelte Laufdauer → (fast) gleicher Spitzenspeicher; Spitze ≪ Datenmenge des Laufs."""
    mk = lambda T: _cfg([BlockCfg(id="kv_00", kind="vacuum", duration_s=T, periodic_rate=2000.0),
                         BlockCfg(id="a_00", kind="heralded", duration_s=T)])
    p1, n1 = _peak(mk(2.0), tmp_path / "t1", 0.25)
    p2, n2 = _peak(mk(4.0), tmp_path / "t2", 0.25)
    assert n2 > 1.8 * n1
    data_bytes_2 = n2 * 1000 * 2
    assert p2 < 1.25 * p1 + 2e6, (p1, p2)                  # konstant (bis auf Rauschen)
    assert p2 < 0.6 * data_bytes_2 * 8                     # deutlich unter dem Speicherbedarf des Datei-Modus
    s = load_session(tmp_path / "t2")
    assert not s.warnings and not validate_digitizer_file(tmp_path / "t2" / "digitizer.h5")
    assert [r.n_records for r in s.runs][0] == 8000        # 2 kHz × 4 s, Periodenraster über Scheibengrenzen
    assert s.manifest["device_modes"] == {"harp": "sim", "digitizer": "sim"} and s.manifest["streaming"]["slice_s"] == 0.25
    kv = s.runs[0]
    t = kv.trigger_D()
    assert np.allclose(np.diff(t), 1 / 2000.0, atol=1e-9)  # keine Lücken an den Scheibengrenzen
    hs = kv.harp
    mk_s = hs.role_s("sync_marker")
    assert 3400 < len(mk_s) < 3800 and np.all(np.diff(mk_s) > 0)   # 1 kHz, ~10 % ausgelassen, über 4 s monoton
    assert mk_s.max() < 4.0 + 1e-6


def test_lazy_walk_and_decimator():
    w = LazyWalk(0.01, 0.05, 0.0, np.random.default_rng(1), seg=64)
    t = np.linspace(0, 100, 5000)
    v = np.concatenate([w(t[i:i + 50]) for i in range(0, 5000, 50)])
    assert len(w.segs) <= 4 and v[0] == 0.0 and np.std(np.diff(v)) > 0
    with pytest.raises(RuntimeError):
        w(np.array([0.0]))                                   # Fenster schon verworfen
    d = TruthDecimator(16)
    for k in range(1000):
        d.add(k, 0, 0, 0)
    a = d.array()
    assert len(a) <= 16 and a[-1, 0] > 900


def _drift_session(tmp_path, name, lo_total, eta_total):
    T = 3.0
    blocks = [BlockCfg(id="kv_00", kind="vacuum", duration_s=1.5, periodic_rate=4000.0),
              BlockCfg(id="dark_00", kind="electronic", duration_s=0.5, periodic_rate=2000.0)]
    for c in range(4):
        blocks += [BlockCfg(id=f"a_{c:02d}", kind="heralded", duration_s=T),
                   BlockCfg(id=f"kv_{c + 1:02d}", kind="vacuum", duration_s=1.5, periodic_rate=4000.0)]
    cfg = _cfg(blocks, L=2000, pre=800, lo_total=lo_total, eta_total=eta_total, sid=name)
    mon = DriftMonitor(fs=cfg.fs, window_s=2.0)
    cur = [None]
    tu = cfg.time_unit

    def on_slice(info, dg, t):
        if cur[0] != info["block_id"]:
            if cur[0] is not None:
                mon.end_run()
            mon.begin_run(info["block_id"], info["kind"], t)
            cur[0] = info["block_id"]
        mon.feed(dg.data, dg.timestamp * tu, (dg.timestamp + dg.record_start) * tu, dg.data_b, t)

    man = StreamingSimulator(cfg, slice_s=0.5, on_slice=on_slice).run(tmp_path / name)
    mon.end_run()
    return mon, man


def test_streaming_drift_detected(tmp_path):
    """LO-Drift 2 % und η-Drift −35 % über die Sitzung werden im Datenstrom erkannt; ohne Drift kein Alarm."""
    mon, man = _drift_session(tmp_path, "drift", 0.02, -0.35)
    tr = mon.trend()
    lo_true = {r["id"]: r["lo_scale"] for r in man["truth"]}
    kv_last = [r for r in mon.runs if r.kind == "vacuum"][-1]
    assert abs(kv_last.lo_rel - lo_true[kv_last.block_id] / lo_true["kv_00"]) < 0.003     # LO-Monitor (Kanal B)
    assert any("LO-Leistung" in w for w in mon.warnings)                                  # > 1 % gemeldet
    assert tr["lo"]["z"] > 5
    eta = {r["id"]: r["eta_state"] for r in man["truth"]}
    A = [r for r in mon.runs if r.kind == "heralded"]
    true_rel = eta[A[-1].block_id] / eta[A[0].block_id] - 1
    assert tr["eta"]["z"] < -3.0, tr["eta"]                                               # η-Drift signifikant
    sig = tr["eta"]["slope_err"] * tr["eta"]["span_s"]
    assert abs(tr["eta"]["change_over_span"] - true_rel) < 3 * sig, (tr["eta"], true_rel)  # Größe stimmt (≤ 3σ)
    ctrl, _ = _drift_session(tmp_path, "ctrl", 0.0, 0.0)
    tc = ctrl.trend()
    assert abs(tc["eta"]["z"]) < 3.0 and not any("LO-Leistung" in w for w in ctrl.warnings)
    assert abs(tc["lo"]["last"] - 1) < 0.003
