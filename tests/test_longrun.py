"""Lange Sitzungen: Wiedergabe, Datenstrom-Auswertung und Drift über Wiedergabe mit beschränktem Speicher.

Zwei Sitzungen (Standard-Laufplan, Laufdauern ×1 und ×2) aus dem Datenstrom-Simulator; die doppelte Länge darf
den Spitzenspeicher (tracemalloc) nicht nennenswert erhöhen, und die Datenstrom-Auswertung muss mit der
Auswertung im Speicher übereinstimmen.
"""
from __future__ import annotations

import gc
import json
import tracemalloc

import numpy as np
import pytest

from hhd.analysis.pipeline import AnalysisParams, analyze_session
from hhd.analysis.replaydrift import drift_over_replay
from hhd.analysis.streaming import analyze_session_streaming
from hhd.io import ptu as P
from hhd.server.devices import ReplayBackend
from hhd.sim.config import BlockCfg, SimConfig
from hhd.sim.streaming import StreamingSimulator
from hhd.sync.pipeline import sync_session


def _gen(out, scale, sid, L=1000, pre=400):
    cfg = SimConfig.load("config/standard.yaml")
    cfg.digitizer.record_length, cfg.digitizer.pretrigger = L, pre
    for b in cfg.blocks:
        b.duration_s = round(b.duration_s * scale / 0.25) * 0.25 or 0.25
    cfg.session.id = sid
    StreamingSimulator(cfg, slice_s=0.25).run(out)
    sync_session(out, None, out / "sync")
    return out


@pytest.fixture(scope="module")
def sessions(tmp_path_factory):
    d = tmp_path_factory.mktemp("longrun")
    return _gen(d / "s1", 1.0, "LONG-1"), _gen(d / "s2", 2.0, "LONG-2")


def _peak(fn):
    gc.collect()
    tracemalloc.start()
    try:
        out = fn()
        return tracemalloc.get_traced_memory()[1], out
    finally:
        tracemalloc.stop()


def _replay(path, check_raw=False):
    be = ReplayBackend(path, chunk_s=0.1, realtime=False)
    specs = be.prepare()
    ok, tot = True, 0
    for i, _ in enumerate(specs):
        be.start_run(i)
        nrec = nraw = 0
        raws, done = [], [False, False]
        while True:
            h, d = be.read("harp"), be.read("digitizer")
            if h is None and d is None:
                break
            if h is not None:
                nraw += len(h.records); done[0] = h.run_done
                if check_raw:
                    raws.append(h.records)
            if d is not None:
                nrec += len(d.data); done[1] = d.run_done
        run = be.session.runs[i]
        good = nrec == run.n_records and nraw == int(run.ptu_tags["TTResult_NumberOfRecords"]) and all(done)
        if check_raw:
            good &= np.array_equal(np.concatenate(raws), P.read_ptu(run.ptu_path).records)
        ok &= good
        tot += nrec
    return ok, tot


def _peak_where(path) -> dict:
    """Diagnose bei Überschreitung: größte Speicherspitze je Abschnitt (Öffnen, Laufstart, Lesen) mit Fundstelle."""
    gc.collect()
    tracemalloc.start()
    worst = {}

    def note(tag, **kw):
        cur, pk = tracemalloc.get_traced_memory()
        if pk > worst.get(tag, (0,))[0]:
            worst[tag] = (pk, cur, kw)
        tracemalloc.reset_peak()
    try:
        be = ReplayBackend(path, chunk_s=0.1, realtime=False)
        specs = be.prepare()
        note("open")
        for i, _ in enumerate(specs):
            be.start_run(i)
            note("start_run", run=i)
            k = 0
            while True:
                h, d = be.read("harp"), be.read("digitizer")
                note("read", run=i, step=k, harp=None if h is None else len(h.records),
                     dig=None if d is None else tuple(d.data.shape))
                k += 1
                if h is None and d is None:
                    break
        return worst
    finally:
        tracemalloc.stop()


def test_replay_complete_and_bounded(sessions):
    sa, sb = sessions
    _replay(sa); _replay(sb)        # Aufwärmen: einmalige Kosten (Importe, erste Dateiöffnung; Windows/3.13 ≈ 4 MB) nicht mitmessen
    p0, (ok0, n0) = _peak(lambda: _replay(sa))
    p1, (ok1, n1) = _peak(lambda: _replay(sb))
    assert ok0 and ok1 and n1 > 1.5 * n0
    assert p1 < 1.25 * p0 + 2e6, (p0, p1, _peak_where(sa), _peak_where(sb))   # unabhängig von der Sitzungsdauer
    assert _replay(sa, check_raw=True)[0]                        # PTU bitgleich wiedergegeben


def test_streaming_analysis_bounded(sessions):
    sa, sb = sessions
    ap = AnalysisParams(n_boot=10)
    p0, r0 = _peak(lambda: analyze_session_streaming(sa, ap, write=False, chunk=256))
    p1, r1 = _peak(lambda: analyze_session_streaming(sb, ap, write=False, chunk=256))
    assert r1["reconstruction"]["A"]["n"] > 1.5 * r0["reconstruction"]["A"]["n"]
    assert p1 < 1.25 * p0 + 3e6, (p0, p1)
    assert p1 < 60e6


def test_streaming_matches_in_memory(sessions, tmp_path):
    _, sb = sessions
    ap = AnalysisParams(n_boot=60)
    m = analyze_session(sb, ap, out_dir=tmp_path / "mem")
    s = analyze_session_streaming(sb, ap, out_dir=tmp_path / "stream")
    for k in ("q0", "var_kv", "var_el", "dark_b", "p_ref"):
        assert s["calibration"][k] == pytest.approx(m["calibration"][k], rel=1e-9, abs=1e-9), k
    for g in ("A", "B"):
        a, b = m["reconstruction"][g], s["reconstruction"][g]
        assert a["n"] == b["n"]
        assert b["var_x"] == pytest.approx(a["var_x"], rel=1e-9)               # exakte Potenzsummen
        assert b["W00_moments"] == pytest.approx(a["W00_moments"], rel=1e-6, abs=1e-9)
        for q in ("W00", "eta_fit", "mean_n"):                                  # MaxLik auf Bins 0.05
            assert abs(b[q] - a[q]) < max(0.25 * a[q + "_err"], 2e-3), (g, q, a[q], b[q])
        for q in ("var_x", "W00", "eta_fit"):
            if a[q + "_err"] > 0:
                assert 0.5 < b[q + "_err"] / a[q + "_err"] < 2.0, (g, q)       # Bootstrap-Fehler ähnlich
    assert s["comparison"]["diff_var_x"]["value"] == pytest.approx(m["comparison"]["diff_var_x"]["value"], rel=1e-9)
    assert s["comparison"]["histogram"]["chi2"] == pytest.approx(m["comparison"]["histogram"]["chi2"], rel=1e-6)
    assert (tmp_path / "stream" / "result.json").exists() and json.loads(
        (tmp_path / "stream" / "result.json").read_text(encoding="utf-8"))["streaming"]


def test_cli_streaming_and_drift(sessions, tmp_path):
    from hhd.cli import main
    sa, _ = sessions
    assert main(["compare", str(sa), "--streaming", "--n-boot", "5", "-o", str(tmp_path / "o")]) == 0
    assert (tmp_path / "o" / "result.json").exists()
    assert main(["drift", str(sa), "--json"]) == 0


def _drift_cfg(sid, lo_total, eta_total, dark_s=0.5):
    cfg = SimConfig()
    cfg.digitizer.record_length, cfg.digitizer.pretrigger = 2000, 800
    blocks = [BlockCfg(id="kv_00", kind="vacuum", duration_s=1.5, periodic_rate=4000.0),
              BlockCfg(id="dark_00", kind="electronic", duration_s=dark_s, periodic_rate=2000.0)]
    for c in range(4):
        blocks += [BlockCfg(id=f"a_{c:02d}", kind="heralded", duration_s=3.0),
                   BlockCfg(id=f"kv_{c + 1:02d}", kind="vacuum", duration_s=1.5, periodic_rate=4000.0)]
    cfg.blocks = blocks
    tot = sum(b.duration_s + cfg.session.gap_s for b in blocks)
    cfg.drift.lo_rel_per_s, cfg.drift.eta_rel_per_s = lo_total / tot, eta_total / tot
    cfg.session.id = sid
    return cfg


def test_drift_over_replay(tmp_path):
    """LO-Drift 2 % / η-Drift −35 % werden über die Wiedergabe erkannt; ohne Drift kein Alarm."""
    StreamingSimulator(_drift_cfg("DRIFT", 0.02, -0.35), slice_s=0.5).run(tmp_path / "d")
    # Dunkellauf länger als das Fenster: LO aus darf keine LO-Warnung geben (0.13, vorher −600 %)
    StreamingSimulator(_drift_cfg("CTRL", 0.0, 0.0, dark_s=5.0), slice_s=0.5).run(tmp_path / "c")
    d = drift_over_replay(tmp_path / "d", chunk_s=0.5, window_s=2.0)
    tr = d["trend"]
    assert tr["lo"]["z"] > 5 and abs(tr["lo"]["last"] - 1.0) > 0.01
    assert any("LO-Leistung" in w for w in d["warnings"])
    assert tr["eta"]["z"] < -3.0, tr["eta"]
    c = drift_over_replay(tmp_path / "c", chunk_s=0.5, window_s=2.0)
    assert abs(c["trend"]["eta"]["z"]) < 3.0 and abs(c["trend"]["lo"]["last"] - 1) < 0.003
    assert not any("LO-Leistung" in w for w in c["warnings"])


def _pca_equal(a, b):
    assert a and b and a["available"] and b["available"]
    assert (a["n_A"], a["n_V"], a["bins"]) == (b["n_A"], b["n_V"], b["bins"])
    for k in ("lambda1", "ratio_l2_l1", "overlap_param", "overlap_noise_expected", "overlap_param_corrected",
              "gamma_fit", "centre_shift_ns"):
        assert b[k] == pytest.approx(a[k], rel=1e-7, abs=1e-10), k
    assert np.allclose(a["mode"], b["mode"], atol=1e-7)


@pytest.fixture(scope="module")
def session_pca(tmp_path_factory):
    return _gen(tmp_path_factory.mktemp("pca") / "s", 1.0, "LONG-PCA", L=2000, pre=800)   # Fenster ±320 ns passt


@pytest.mark.parametrize("cap", [20000, 300, 0])
def test_streaming_pca_matches_in_memory(session_pca, tmp_path, cap):
    """Inkrementelle Fensterkovarianz der Datenstrom-Auswertung ≡ In-Memory-PCA (gleiche Auswahl, Obergrenze)."""
    sb = session_pca
    ap = AnalysisParams(n_boot=2, pca_max_records=cap)
    m = analyze_session(sb, ap, out_dir=tmp_path / "mem")
    s = analyze_session_streaming(sb, ap, write=False)
    _pca_equal(m["pca"], s["pca"])
    if cap == 300:
        assert s["pca"]["n_A"] <= 300 and s["pca"]["n_V"] <= 300
    assert s["pca"]["streaming"]["incremental_covariance"]
    assert s["efficiency_budget"]["eta_mode_pca"] == pytest.approx(m["efficiency_budget"]["eta_mode_pca"], rel=1e-7)
    if cap != 300:                                                      # 300 Fenster: zu verrauscht
        assert 0.9 < s["pca"]["overlap_param_corrected"] <= 1.0
