"""Mess-Server (0.9): Geräteabstraktion, ED-8b-Rechte, Steuersperre, Protokoll, Zugang und Ende-zu-Ende-Test
mit simulierten Geräten (Sitzung starten → Dateien → Sync → Analyse → Ergebnis über die API der App)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from hhd.server.app import create_app  # noqa: E402
from hhd.server.control import CommandError, ControlLock, ParamPolicy, is_local, safe_session_dir  # noqa: E402
from hhd.server.devices import (DeviceError, SimBackend, SimDigitizer, make_devices, short_plan)  # noqa: E402
from hhd.server.service import MeasurementService, ServerConfig, load_config  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _cfg(tmp_path, **kw) -> ServerConfig:
    c = ServerConfig(data_dir=str(tmp_path / "data"), log_dir=str(tmp_path / "logs"), realtime=False,
                     analysis_inprocess=True, n_boot=10, sim_config=str(ROOT / "config/standard.yaml"), status_hz=5)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_short_plan_and_sim_stream():
    be, h, d = make_devices("sim", base_config=ROOT / "config/standard.yaml", realtime=False)
    cfg = be.make_config("T-S", 11, "kurz")
    kinds = [b.kind for b in cfg.blocks]
    assert kinds == ["vacuum", "electronic", "herald_blocked", "heralded", "free_running", "heralded", "vacuum"]
    runs = be.prepare(cfg)
    h.open(); d.open()
    i = kinds.index("heralded")
    be.start_run(i)
    for x in (h, d):
        x.arm(runs[i]); x.start()
    nh = nr = nm = 0
    while True:
        a, b = h.read(), d.read()
        if a is None and b is None:
            break
        if a is not None:
            nh += len(a.herald_s); nm += len(a.markers_s)
        if b is not None:
            nr += len(b.trig_D)
            assert b.data.ndim == 2 and len(b.first_D) == len(b.trig_D)
    assert nh > 300 and nm > 50
    assert abs(nr - nh) <= 0.02 * nh            # heralded: ein Record je Herald (Totzeit vernachlässigbar)


def test_live_device_without_vendor_library(monkeypatch, tmp_path):
    from hhd.server.livedev import LiveCfg
    monkeypatch.delenv("HHD_HH500LIB", raising=False); monkeypatch.delenv("HHD_ADQAPI", raising=False)
    lc = LiveCfg(hh500_lib=str(tmp_path / "fehlt" / "libhh500lib.so"), adq_lib=str(tmp_path / "fehlt" / "libadq.so"))
    _, h, d = make_devices("live", live_cfg=lc)
    with pytest.raises(DeviceError, match="Herstellerbibliothek nicht gefunden.*HH500Lib"):
        h.open()
    with pytest.raises(DeviceError, match="Herstellerbibliothek nicht gefunden.*SP-Devices-SDK"):
        d.open()


def test_param_policy_ed8b():
    d = SimDigitizer(SimBackend())
    pol = ParamPolicy()
    assert pol.check(d, {"preview_records": 500}, False, "c1", None)[0] == "ok"          # unkritisch
    assert pol.check(d, {"input_range_mV": 250.0}, True, "c1", None)[0] == "ok"          # lokal
    v, code = pol.check(d, {"input_range_mV": 250.0}, False, "c1", None)
    assert v == "confirm"
    with pytest.raises(CommandError, match="passt nicht"):
        pol.check(d, {"input_range_mV": 250.0}, False, "c2", code)                         # anderer Client
    v, code = pol.check(d, {"input_range_mV": 250.0}, False, "c1", None)
    with pytest.raises(CommandError, match="passt nicht"):
        pol.check(d, {"input_range_mV": 1000.0}, False, "c1", code)                        # andere Änderung
    v, code = pol.check(d, {"input_range_mV": 250.0}, False, "c1", None)
    assert pol.check(d, {"input_range_mV": 250.0}, False, "c1", code)[0] == "ok"
    with pytest.raises(CommandError, match="abgelaufen"):
        pol.check(d, {"input_range_mV": 250.0}, False, "c1", code)                         # nur einmal gültig
    with pytest.raises(CommandError, match="nur am Mess-PC"):
        pol.check(d, {"trigger_output": True}, False, "c1", None)
    with pytest.raises(CommandError, match="erlaubt"):
        pol.check(d, {"input_range_mV": 333.0}, True, "c1", None)
    with pytest.raises(CommandError, match="unbekannt"):
        pol.check(d, {"foo": 1}, True, "c1", None)


def test_is_local_and_lock(tmp_path):
    assert is_local({}, "127.0.0.1")
    assert not is_local({"cf-connecting-ip": "1.2.3.4"}, "127.0.0.1")       # Tunnel gilt als entfernt
    assert not is_local({"x-forwarded-for": "1.2.3.4"}, "::1")
    assert not is_local({}, "192.168.1.20")
    lk = ControlLock(timeout_s=0.2)
    assert lk.acquire("a") and not lk.acquire("b") and lk.check("a") and not lk.check("b")
    time.sleep(0.25)
    assert lk.acquire("b")                                                  # verfallen
    with pytest.raises(CommandError):
        safe_session_dir(tmp_path, "../etc")


def test_load_config(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text('[server]\nport = 9000\ntoken = "abc"\n[devices]\ndevice_mode = "replay"\n', encoding="utf-8")
    c = load_config(p)
    assert c.port == 9000 and c.token == "abc" and c.device_mode == "replay"
    p.write_text('[server]\nfoo = 1\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unbekannt"):
        load_config(p)
    assert load_config(ROOT / "config/server.toml").port == 8766


def _wait(svc, t=600):
    assert svc.wait_idle(t)


def test_end_to_end_sim(tmp_path):
    cfg = _cfg(tmp_path)
    app = create_app(cfg, tmp_path)
    svc = app.state.svc
    with TestClient(app) as cl:
        assert cl.get("/api/health").json()["ok"]
        assert cl.get("/").status_code == 200 and "Harp ADQ32 HHD Mobil" in cl.get("/").text
        assert cl.get("/api/status").json()["simulated"] is True
        with cl.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello" and hello["local"] is False      # TestClient = entfernt
            assert ws.receive_json()["type"] == "status"

            def cmd(c, params=None, i=[0]):
                i[0] += 1
                ws.send_json(dict(type="command", cmd=c, params=params or {}, id=i[0]))
                while True:
                    m = ws.receive_json()
                    if m.get("type") == "ack" and m.get("id") == i[0]:
                        return m

            assert not cmd("start_session", {"plan": "kurz"})["ok"]          # ohne Steuersperre
            assert not cmd("rm_rf")["ok"]
            assert cmd("acquire_control")["ok"]
            m = cmd("start_session", {"plan": "kurz"})
            assert m["ok"], m
            sid = m["session_id"]
            assert not cmd("start_session", {"plan": "kurz"})["ok"]          # läuft schon
            _wait(svc)
            # kritischer Parameter aus der Ferne: zweistufig
            m = cmd("set_device", {"device": "digitizer", "params": {"input_range_mV": 250.0}})
            assert not m["ok"] and m["needs_confirm"]
            m2 = cmd("set_device", {"device": "digitizer", "params": {"input_range_mV": 250.0}, "confirm": m["confirm"]})
            assert m2["ok"]
            assert not cmd("set_device", {"device": "harp", "params": {"trigger_output": True}})["ok"]
            assert cmd("set_display", {"hist_range": 5})["ok"]
        # Trennung gibt die Sperre frei
        assert svc.lock.state()["holder"] is None
        sess = cl.get("/api/sessions").json()
        assert sess[0]["id"] == sid and sess[0]["analyzed"] and sess[0]["runs"] == 7
        d = tmp_path / "data" / sid
        for f in ("session.json", "digitizer.h5", "harp_a_01.ptu", "server_session.json", "live_preview.json",
                  "sync/sync.json", "analysis/result.json", "analysis/report.md"):
            assert (d / f).exists(), f
        sm = cl.get(f"/api/sessions/{sid}/summary").json()
        A, B = sm["groups"]["A"], sm["groups"]["B"]
        assert A["W00"] < 0 < B["W00"] and A["n"] > 1000
        assert len(A["wigner_r"]) == len(A["wigner_radial"]) and len(A["p_n"]) > 2
        assert "histogram" in sm["comparison"] and sm["efficiency_budget"]["eta_el"] is not None
        assert "# Auswertung" in cl.get(f"/api/sessions/{sid}/report").text
        assert cl.get("/api/sessions/..%2Fetc/result").status_code == 404
        live = cl.get(f"/api/sessions/{sid}/live").json()
        kinds = {r["kind"]: r for r in live["runs"]}
        assert all(r["done"] for r in live["runs"]) and len(live["runs"]) == 7
        assert kinds["heralded"]["var"] > 0.8 and abs(kinds["free_running"]["var"] - 0.5) < 0.1
        assert kinds["heralded"]["sync"]["locked"] and kinds["heralded"]["sync"]["matched_frac"] > 0.95
        log = cl.get("/api/log").json()
        cmds = [(e["cmd"], e["ok"]) for e in log]
        assert ("start_session", False) in cmds and ("start_session", True) in cmds and ("rm_rf", False) in cmds
        assert all(e["local"] is False for e in log)
        assert all(e["params"].get("confirm") in (None, "***") for e in log)
        lines = (tmp_path / "logs" / "commands.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == len(log)
        r = cl.get("/")
        assert r.headers["x-content-type-options"] == "nosniff" and "frame-ancestors" in r.headers["content-security-policy"]


def test_stop_and_replay(tmp_path):
    cfg = _cfg(tmp_path, auto_analyze=False, realtime=True, speed=2.0)
    svc = MeasurementService(cfg, tmp_path)
    assert svc.handle("c", "x", True, "acquire_control", {})["ok"]
    sid = svc.handle("c", "x", True, "start_session", {"plan": "kurz"})["session_id"]
    t0 = time.time()
    while len(svc.live.runs) < 3 and time.time() - t0 < 60:
        time.sleep(0.05)
    assert svc.handle("c", "x", True, "stop_session", {})["ok"]
    _wait(svc, 60)
    assert svc.state == "abgebrochen"
    meta = json.loads((tmp_path / "data" / sid / "server_session.json").read_text(encoding="utf-8"))
    assert meta["stopped"] and 1 <= meta["runs_done"] < 7
    svc.close()
    # vollständige Kurzsitzung erzeugen und wiedergeben
    be = SimBackend(ROOT / "config/standard.yaml", realtime=False)
    c = be.make_config("SRC-01", 77, "kurz")
    be.prepare(c)
    src = tmp_path / "src"
    be.write_files(src)
    rcfg = _cfg(tmp_path / "r", device_mode="replay", replay_dir=str(src), auto_analyze=True)
    rs = MeasurementService(rcfg, tmp_path)
    rs.handle("c", "x", True, "acquire_control", {})
    m = rs.handle("c", "x", True, "start_session", {"plan": "kurz"})
    assert m["ok"], m
    _wait(rs)
    assert rs.state == "fertig", rs.msg
    out = tmp_path / "r" / "data" / m["session_id"]
    assert not (out / "truth.h5").exists() and (out / "digitizer.h5").is_symlink()
    runs = rs.live.snapshot()["runs"]
    assert len(runs) == 7 and all(r["done"] for r in runs)
    a = [r for r in runs if r["kind"] == "heralded"]
    assert all(r["var"] > 0.8 for r in a)
    assert (out / "analysis" / "result.json").exists()
    rs.close()


def test_auth_token(tmp_path):
    cfg = _cfg(tmp_path, token="ABCD-2345")
    app = create_app(cfg, tmp_path)
    with TestClient(app) as cl:
        assert cl.get("/api/status").status_code == 401
        assert cl.get("/api/health").status_code == 200
        r = cl.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
        with pytest.raises(Exception):
            with cl.websocket_connect("/ws") as ws:
                ws.receive_json()
        assert cl.post("/login", data={"code": "falsch", "next": "/"}).status_code == 401
        r = cl.post("/login", data={"code": "abcd2345", "next": "/"}, follow_redirects=False)
        assert r.status_code == 303
        assert cl.get("/api/status").status_code == 200                       # Cookie gesetzt
    with TestClient(create_app(cfg, tmp_path)) as c2:
        r = c2.get("/api/status?token=ABCD2345", follow_redirects=False)
        assert r.status_code == 303 and "hhd_session" in r.headers.get("set-cookie", "")
        with pytest.raises(Exception):                                       # fremde Origin
            with c2.websocket_connect("/ws?token=ABCD2345", headers={"origin": "http://evil.example"}) as ws:
                ws.receive_json()
        with c2.websocket_connect("/ws?token=ABCD2345") as ws:
            assert ws.receive_json()["type"] == "hello"


def test_stream_sim_engine(tmp_path):
    """Simulator im Datenstrom als Server-Gerät (sim_engine = stream): Dateien wie im Live-Betrieb geschrieben,
    Wahrheit dezimiert, Auswertung läuft, Art in Status/Geräteinfo/Sitzung sichtbar, umschaltbar ohne Sitzung."""
    import gc
    import tracemalloc
    from hhd.server.devices import StreamSimBackend
    cfg = _cfg(tmp_path, sim_engine="stream", sim_record_truth=True)
    svc = MeasurementService(cfg, tmp_path)
    st = svc.status()
    assert st["sim_engine"] == "stream" and isinstance(svc.backend, StreamSimBackend)
    assert "Datenstrom" in st["devices"]["digitizer"]["model"] and st["devices"]["harp"]["sim_engine"] == "stream"
    assert svc.handle("c", "x", True, "acquire_control", {})["ok"]
    m = svc.handle("c", "x", True, "start_session", {"plan": "kurz"})
    assert m["ok"], m
    assert not svc.handle("c", "x", True, "set_display", {"sim_engine": "batch"})["ok"]     # nicht während der Messung
    _wait(svc)
    assert svc.state == "fertig", svc.msg
    d = tmp_path / "data" / m["session_id"]
    for f in ("session.json", "digitizer.h5", "harp_a_01.ptu", "truth.h5", "sync/sync.json", "analysis/result.json"):
        assert (d / f).exists(), f
    man = json.loads((d / "session.json").read_text(encoding="utf-8"))
    assert man["streaming"]["generator"] == "hhd.sim.streaming" and len(man["truth"]) == 7 and "truth.h5" in man["files"]
    assert json.loads((d / "server_session.json").read_text(encoding="utf-8"))["sim_engine"] == "stream"
    from hhd.sim.record_validate import validate_records
    assert "truth_records.h5" in man["files"]
    vr = validate_records(d)["summary"]
    assert vr["misassigned"] == 0 and vr["n_matched"] > 100 and vr["efficiency"] > 0.95, vr
    from hhd.io.session import load_session
    s = load_session(d)
    assert not s.warnings and len(s.runs) == 7
    j = list(svc.jobs.values())[0]
    assert j.state == "fertig", j.error
    runs = svc.live.snapshot()["runs"]
    a = [r for r in runs if r["kind"] == "heralded"]
    assert all(r["var"] > 0.8 for r in a) and all(r["sync"]["matched_frac"] > 0.95 for r in a)
    # umschalten (ohne laufende Sitzung) und zurück
    assert svc.handle("c", "x", True, "set_display", {"sim_engine": "batch"})["ok"]
    assert svc.status()["sim_engine"] == "batch" and not isinstance(svc.backend, StreamSimBackend)
    assert svc.harp.backend is svc.backend and svc.dig.backend is svc.backend
    assert not svc.handle("c", "x", True, "set_display", {"sim_engine": "xyz"})["ok"]
    assert svc.handle("c", "x", True, "set_display", {"sim_engine": "stream"})["ok"]
    svc.close()

    # Speicher: doppelte Laufdauer → gleiche Spitze (Batch-Simulator wächst)
    def peak(engine, scale):
        c = _cfg(tmp_path / f"{engine}{scale}", sim_engine=engine, time_scale=scale, auto_analyze=False)
        sv = MeasurementService(c, tmp_path)
        sv.handle("c", "x", True, "acquire_control", {})
        gc.collect()
        tracemalloc.start()
        try:
            sv.handle("c", "x", True, "start_session", {"plan": "kurz"})
            _wait(sv)
            assert sv.state == "fertig", sv.msg
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
            sv.close()
    s1, s2 = peak("stream", 1.0), peak("stream", 2.0)
    b2 = peak("batch", 2.0)
    assert s2 < 1.2 * s1 + 3e6, (s1, s2)
    assert s2 < 0.6 * b2, (s2, b2)
