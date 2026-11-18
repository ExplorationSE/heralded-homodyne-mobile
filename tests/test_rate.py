"""Zerlege-Prozess und Ratenbegrenzung (0.15): Prozess = seriell, Rückstau, Absturz, Grenzwert, Selbsttest,
Testlast im Simulator."""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from hhd.io import ptu as P
from hhd.io.t2split import SplitOverload, SplitProcessError, T2SplitProcess, split_serial, synthetic_t2
from hhd.server.rateguard import HARP_MAX_SUSTAINED_MCPS, RateGuard, selftest

ROOT = Path(__file__).resolve().parents[1]
ROLES = ({0: "sync_marker", 1: "herald"}, {})


def _collect(sp, blocks, poll_every=1):
    out = []
    for i, b in enumerate(blocks):
        sp.submit(b)
        if i % poll_every == 0:
            out += sp.poll()
    return out + sp.finish_run(60)


def _merge(res):
    res = sorted(res, key=lambda r: r.seq)
    return (np.concatenate([r.herald_s for r in res]), np.concatenate([r.markers_s for r in res]),
            res[-1].last_ps, sum(r.n_records for r in res))


def test_process_equals_serial():
    rec = synthetic_t2(5.0, 0.2, seed=3)                      # 1 Mio. Ereignisse, Sync-Marker auf Kanal 0
    cuts = np.sort(np.random.default_rng(1).choice(len(rec), 12, replace=False))
    blocks = np.split(rec, cuts)                               # ungleich große Blöcke, auch leere
    blocks.insert(3, rec[:0])
    ref = _merge(split_serial(blocks, ROLES))
    sp = T2SplitProcess(n_slots=3, slot_mrec=0.05)            # Blöcke > Slot → auf mehrere Slots verteilt
    try:
        sp.begin_run(ROLES)
        got = _merge(_collect(sp, blocks))
        for a, b in zip(got, ref):
            assert np.array_equal(a, b)
        # zweiter Lauf: Dekoder zurückgesetzt, gleiches Ergebnis
        sp.begin_run(ROLES)
        got2 = _merge(_collect(sp, blocks, poll_every=5))
        assert np.array_equal(got2[0], ref[0]) and got2[2] == ref[2]
    finally:
        sp.close()
    assert ref[0].size > 0.8e6 and ref[1].size == 200


def test_backpressure_never_blocks_then_overload():
    rec = synthetic_t2(10.0, 0.1, seed=4)                     # 4 MB je Block
    sp = T2SplitProcess(n_slots=2, slot_mrec=0.01, max_backlog_mb=30)
    try:
        sp.begin_run(ROLES)
        t0 = time.perf_counter()
        with pytest.raises(SplitOverload, match="Ereignisrate zu hoch"):
            for _ in range(20):
                sp.submit(rec)
        assert time.perf_counter() - t0 < 2.0                  # submit wartet nie auf den Prozess
        assert sp.backlog_bytes > 30e6 and sp.pending() > 0
        # neuer Lauf: Rückstau des abgebrochenen Laufs fällt weg, Ergebnisse stimmen
        sp.begin_run(ROLES)
        got = _merge(_collect(sp, [rec]))
        ref = _merge(split_serial([rec], ROLES))
        assert np.array_equal(got[0], ref[0]) and got[3] == len(rec)
    finally:
        sp.close()


def test_process_crash_is_loud(monkeypatch):
    monkeypatch.setenv("HHD_T2SPLIT_CRASH_AT", "2")
    rec = synthetic_t2(2.0, 0.05, seed=5)
    sp = T2SplitProcess(n_slots=4, slot_mrec=0.05)
    try:
        sp.begin_run(ROLES)
        with pytest.raises(SplitProcessError, match="Exit-Code 3"):
            for _ in range(10):
                sp.submit(rec)
                sp.poll()
            sp.finish_run(20)
        with pytest.raises(SplitProcessError):                 # bleibt im Fehlerzustand, kein Rückfall
            sp.submit(rec)
    finally:
        sp.close()


def test_rateguard_warn_and_stop():
    g = RateGuard(limit_mcps=1.0, window_s=0.5, stop_s=2.0)
    g.reset()
    st = [g.feed(50_000, 0.1 * (i + 1)) for i in range(10)]   # 0.5 Mcps
    assert set(st) == {"ok"} and abs(g.rate_mcps - 0.5) < 0.01
    st = [g.feed(300_000, 1.0 + 0.1 * (i + 1)) for i in range(15)]   # 3 Mcps für 1.5 s → Warnung
    assert st[-1] == "warn" and g.n_warn == 1
    st = [g.feed(300_000, 2.5 + 0.1 * (i + 1)) for i in range(10)]
    assert st[-1] == "stop" and g.over_s_total >= 2.0 and g.peak_mcps > 2.9
    g.reset()
    assert g.feed(100, 0.1) == "ok"
    g0 = RateGuard(limit_mcps=0.0)                              # kein Grenzwert: nie Warnung
    g0.reset()
    assert g0.feed(10**9, 1.0) == "ok"


def test_selftest(tmp_path):
    r = selftest(tmp_path, safety=0.5, duration_s=0.02, repeats=3, force=True)
    assert r["split_mcps"] > 0 and r["write_mcps"] > 0 and not r["cached"]
    assert r["limit_mcps"] == pytest.approx(min(0.5 * min(r["split_mcps"], r["write_mcps"]),
                                                HARP_MAX_SUSTAINED_MCPS), abs=0.01)
    assert r["bottleneck"] in ("zerlegen", "schreiben") and not list(tmp_path.glob(".hhd_selftest*"))
    assert selftest(tmp_path, safety=0.5)["cached"]


def test_encode_t2_vectorized_matches_loop():
    rng = np.random.default_rng(8)
    for n, span in ((1000, 10 * P.T2_WRAP), (5000, 3000 * P.T2_WRAP), (50, (P.T2_WRAP - 1) * P.T2_WRAP * 2)):
        t = np.sort(rng.integers(0, span, n))
        k = rng.choice([P.KIND_EVENT, P.KIND_MARKER, P.KIND_SYNC], n)
        c = np.where(k == P.KIND_EVENT, rng.integers(1, 5, n), rng.integers(1, 15, n))
        rec, rt = P.encode_t2(t, k, c)
        ev = P.T2Decoder().decode(rec)
        got = np.sort(np.concatenate([ev.event_time_ps, ev.marker_time_ps]))
        assert np.array_equal(got, t)                          # Rundlauf über Überläufe (auch > 2^25-1 Perioden)
        assert np.all(np.diff(rt) >= 0)


def test_sim_extra_rate_channel():
    from hhd.server.devices import SimBackend
    be = SimBackend(ROOT / "config/standard.yaml", realtime=False, time_scale=0.05)
    cfg0 = be.make_config("T-R", 5, "kurz")
    r0 = be.prepare(cfg0)
    h0 = be.res.blocks[3].harp_records.copy() if hasattr(be.res.blocks[3], "harp_records") else None
    be.harp_extra_hz = 2e5
    cfg1 = be.make_config("T-R", 5, "kurz")
    assert cfg1.harp.extra_rate_hz == 2e5 and cfg1.channel_roles("heralded")["3"] == "rate_test"
    be.prepare(cfg1)
    b0 = r0[3]
    assert b0 is not None
    if h0 is not None:
        ev0 = P.T2Decoder().decode(h0)
        ev1 = P.T2Decoder().decode(be.res.blocks[3].harp_records)
        on1 = lambda e: e.event_time_ps[e.event_channel == 1]
        assert np.array_equal(on1(ev0), on1(ev1))               # Testlast lässt die übrigen Kanäle unverändert
        n3 = int(np.sum(ev1.event_channel == 3))
        assert not np.any(ev0.event_channel == 3)
        T = cfg1.blocks[3].duration_s
        assert abs(n3 - 2e5 * T) < 6 * np.sqrt(2e5 * T) + 0.02 * 2e5 * T


def _svc(tmp_path, **kw):
    from hhd.server.service import MeasurementService, ServerConfig
    c = ServerConfig(data_dir=str(tmp_path / "data"), log_dir=str(tmp_path / "logs"), realtime=True, speed=1.0,
                     analysis_inprocess=True, n_boot=10, sim_config=str(ROOT / "config/standard.yaml"),
                     time_scale=0.1, auto_analyze=False, status_hz=5)
    for k, v in kw.items():
        setattr(c, k, v)
    return MeasurementService(c, tmp_path)


def _start(svc):
    assert svc.handle("c", "127.0.0.1", True, "acquire_control", {})["ok"]
    m = svc.handle("c", "127.0.0.1", True, "start_session", {"plan": "kurz"})
    assert m["ok"], m
    assert svc.wait_idle(300)
    return svc.data_dir / m["session_id"]


def test_sim_overrate_stops_with_message(tmp_path):
    svc = _svc(tmp_path, sim_harp_extra_mcps=0.5, harp_max_mcps=0.2, harp_rate_selftest=False,
               harp_rate_window_s=0.1, harp_rate_stop_s=0.3, realtime=False, time_scale=2.0)
    try:
        st = svc.status()["rate"]
        assert st["limit_mcps"] == 0.2 and "harp_max_mcps" in st["source"]
        assert st["reason"].startswith("Zählrate über dem fest eingestellten Grenzwert"), st["reason"]
        d = _start(svc)
        assert svc.state == "abgebrochen" and "RATENGRENZE" in svc.msg, svc.msg
        assert "fest eingestellten Grenzwert" in svc.msg, svc.msg       # Grund steht in der Meldung (ab 0.15.0)
        sm = svc.status()["rate"]["stopped_msg"]
        assert "Ereignisrate" in sm and "Abschwäch" in sm and "verworfen" in sm, sm
        kinds = [w["kind"] for w in svc.live.warnings]
        assert "rate_stop" in kinds
        assert "0/7 Läufe" in svc.msg and not d.exists()      # erster Lauf angehalten → wie Stopp verworfen
        logs = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "logs").rglob("*") if p.is_file())
        assert "rate_stop" in logs and "Ereignisrate" in logs
        assert svc.status()["rate"]["guard"]["peak_mcps"] > 0.2
    finally:
        svc.close()


def test_sim_below_limit_runs_normally(tmp_path):
    svc = _svc(tmp_path, harp_max_mcps=50.0, harp_rate_selftest=False, realtime=False)
    try:
        _start(svc)
        assert svc.state == "fertig", svc.msg
        assert not [w for w in svc.live.warnings if w["kind"].startswith("rate")]
        assert svc.status()["rate"]["guard"]["state"] == "ok"
    finally:
        svc.close()


def test_live_mock_overrate_and_crash(tmp_path, mock_libs, monkeypatch):
    """Live mit Mock-HH500: zu hohe Rate → Abbruch mit Meldung; Absturz des Zerlege-Prozesses → Fehler (laut)."""
    from hhd.server.service import MeasurementService, ServerConfig

    def svc(sub, **kw):
        c = ServerConfig(data_dir=str(tmp_path / sub / "data"), log_dir=str(tmp_path / sub / "logs"),
                         analysis_inprocess=True, n_boot=10, sim_config=str(ROOT / "config/standard.yaml"),
                         time_scale=0.08, auto_analyze=False, hh500_lib=str(mock_libs["hh500"]),
                         adq_lib=str(mock_libs["adq"]), adq_trace_dir=str(tmp_path / sub / "trace"),
                         device_mode="live", harp_mode="live", digitizer_mode="sim", harp_rate_selftest=False)
        for k, v in kw.items():
            setattr(c, k, v)
        return MeasurementService(c, tmp_path / sub)

    s = svc("over", harp_max_mcps=1e-4, harp_rate_window_s=0.05, harp_rate_stop_s=0.02)   # kurze Läufe (time_scale): Anhalten sicher auch bei grober Leseauflösung (Windows)
    try:
        assert s.status()["modes"]["harp"] == "live"
        _start(s)
        assert s.state == "abgebrochen" and "RATENGRENZE" in s.msg, s.msg
    finally:
        s.close()
    monkeypatch.setenv("HHD_T2SPLIT_CRASH_AT", "1")
    s = svc("crash")
    try:
        _start(s)
        assert s.state == "fehler" and "Zerlege-Prozess" in s.msg, s.msg
    finally:
        s.close()


def test_rate_reason_text():
    """Ab 0.15.0 nennt die Ratenwarnung den Grund (Rechenleistung, Speicher, Gerät, fester Wert)."""
    from hhd.server.service import MeasurementService as M
    st = lambda b: dict(bottleneck=b)
    r = M._rate_reason(dict(limit_mcps=12.0, source="Selbsttest (zerlegen begrenzt)", selftest=st("zerlegen")), 85)
    assert r.startswith("Zählrate zu hoch für die Echtzeitberechnung"), r
    r = M._rate_reason(dict(limit_mcps=12.0, source="Selbsttest (schreiben begrenzt)", selftest=st("schreiben")), 85)
    assert r.startswith("Zählrate zu hoch für die Echtzeitspeicherung"), r
    r = M._rate_reason(dict(limit_mcps=85.0, source="Selbsttest", selftest=st("zerlegen")), 85)
    assert "HydraHarp 500" in r and "85" in r, r
    r = M._rate_reason(dict(limit_mcps=3.0, source="Umgebung HHD_HARP_MAX_MCPS"), 85)
    assert "fest eingestellten" in r and "HHD_HARP_MAX_MCPS" in r, r
    assert M._rate_reason(dict(limit_mcps=0.0, source=""), 85) == ""
