"""Live- und gemischter Betrieb des Mess-Servers mit den Mock-Bibliotheken (HH500Lib, ADQAPI):
Betriebsart je Gerät in allen Dateien, ED-8b beim Start, Rückfall ohne Bibliothek, Pause zwischen Läufen."""
from __future__ import annotations

import json
import time
from pathlib import Path

import h5py
import numpy as np

from hhd.io import ptu as P
from hhd.io.schema import validate_digitizer_file
from hhd.io.session import load_session
from hhd.server.service import MeasurementService, ServerConfig

ROOT = Path(__file__).resolve().parents[1]


def _svc(tmp_path, mock_libs, **kw) -> MeasurementService:
    c = ServerConfig(data_dir=str(tmp_path / "data"), log_dir=str(tmp_path / "logs"), analysis_inprocess=True,
                     n_boot=10, sim_config=str(ROOT / "config/standard.yaml"), time_scale=0.08, auto_analyze=False,
                     hh500_lib=str(mock_libs["hh500"]), adq_lib=str(mock_libs["adq"]),
                     adq_trace_dir=str(tmp_path / "trace"))
    for k, v in kw.items():
        setattr(c, k, v)
    return MeasurementService(c, tmp_path)


def _run(svc, local=True, timeout=120):
    assert svc.handle("c", "127.0.0.1", local, "acquire_control", {})["ok"]
    m = svc.handle("c", "127.0.0.1", local, "start_session", {"plan": "kurz"})
    assert m["ok"], m
    assert svc.wait_idle(timeout)
    assert svc.state == "fertig", svc.msg
    return svc.data_dir / m["session_id"]


def _ptu_tags(p):
    return P.read_ptu_header(p)[0]


def test_live_live_session(tmp_path, mock_libs):
    svc = _svc(tmp_path, mock_libs, device_mode="live")
    st = svc.status()
    assert st["modes"] == {"harp": "live", "digitizer": "live"} and st["mode"] == "live" and not st["simulated"]
    assert st["mode_labels"]["session"] == "LIVE" and st["devices"]["harp"]["hardware"]["n_channels"] == 4
    d = _run(svc)
    msgs = [w["msg"] for w in svc.live.warnings]
    # Mock: LO-Monitor auch im Dunkellauf an → Dunkelwert verworfen statt unsinniger LO-Normierung
    assert any("Dunkelwert verworfen" in m for m in msgs) and not any("LO-Leistung" in m for m in msgs), msgs
    svc.close()
    man = json.loads((d / "session.json").read_text(encoding="utf-8"))
    assert man["device_modes"] == {"harp": "live", "digitizer": "live"} and man["acquisition_mode"] == "live"
    assert man["simulated"] is False and not (d / "truth.h5").exists() and len(man["runs"]) == 7
    assert man["hardware"]["harp"]["model"].startswith("HydraHarp 500") and man["hardware"]["digitizer"]["adq_num"] == 1
    assert validate_digitizer_file(d / "digitizer.h5") == []
    with h5py.File(d / "digitizer.h5") as f:
        assert f.attrs["device_mode_harp"] == "live" and f.attrs["device_mode_digitizer"] == "live"
        dev = f["device"].attrs
        assert dev["channel_b_trigger"] == "SYNC" and dev["time_unit_s"] == 50e-12 and dev["serial_number"] == "SPD-MOCK1"
        g = f["blocks/a_01"]
        assert "data_b" not in g["records"] and g["markers/data_b"].shape[0] == g["markers/timestamp"].shape[0] > 30
        assert g.attrs["device_mode_digitizer"] == "live"
    for r in man["runs"]:
        tags = _ptu_tags(d / r["ptu_file"])
        assert tags["UsrHHD_DeviceMode"] == "live" and tags["UsrHHD_AcquisitionMode"] == "live"
        assert tags["TTResult_NumberOfRecords"] == r["n_harp_records"] > 0 and "(SIMULATED)" not in tags["HW_Type"]
    s = load_session(d)
    assert not s.warnings, s.warnings
    a = s.run("a_01")
    assert a.has_marker_b and not a.has_data_b and a.harp.role_s("herald").size > 30
    from hhd.modes.extract import record_means_b
    mb = record_means_b(a)
    assert np.all(np.isfinite(mb)) and abs(np.mean(mb) - 1700) < 5          # LO-Monitor (Mock: 1700 LSB)
    from hhd.analysis.syncdata import ensure_sync
    ensure_sync(s)                                                           # Synchronisation der Live-Daten
    sj = json.loads((d / "sync" / "sync.json").read_text(encoding="utf-8"))
    kv = sj["runs"]["kv_00"]["marker"]
    assert kv["n_pairs"] == kv["n_markers_H"] > 10 and abs(kv["period_D"] - 1e-3) < 1e-9
    log = [json.loads(x) for x in (tmp_path / "logs" / "commands.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["cmd"] for e in log] == ["acquire_control", "start_session"]


def test_mixed_live_harp_sim_digitizer(tmp_path, mock_libs):
    svc = _svc(tmp_path, mock_libs, harp_mode="live", digitizer_mode="sim", auto_analyze=True)
    assert svc.status()["mode"] == "mixed" and svc.status()["mode_labels"] == {"harp": "LIVE", "digitizer": "SIMULIERT",
                                                                               "session": "GEMISCHT"}
    d = _run(svc)
    assert "keine automatische Auswertung" in svc.msg and not svc.jobs
    svc.close()
    man = json.loads((d / "session.json").read_text(encoding="utf-8"))
    assert man["device_modes"] == {"harp": "live", "digitizer": "sim"} and man["acquisition_mode"] == "mixed"
    assert not (d / "truth.h5").exists() and man["simulated"] is False
    with h5py.File(d / "digitizer.h5") as f:
        assert f.attrs["device_mode_digitizer"] == "sim" and f.attrs["acquisition_mode"] == "mixed"
        assert "data_b" in f["blocks/a_01/records"]                        # simulierter Kanal B wie im Schema
    tags = _ptu_tags(d / "harp_a_01.ptu")
    assert tags["UsrHHD_DeviceMode"] == "live" and json.loads(tags["UsrHHD_DeviceModes"])["digitizer"] == "sim"
    assert validate_digitizer_file(d / "digitizer.h5") == [] and not load_session(d).warnings
    meta = json.loads((d / "server_session.json").read_text(encoding="utf-8"))
    assert meta["acquisition_mode"] == "mixed" and meta["hardware"]["harp"]["serial"]


def test_mixed_sim_harp_live_digitizer_and_sim_metadata(tmp_path, mock_libs):
    svc = _svc(tmp_path, mock_libs, harp_mode="sim", digitizer_mode="live", adq_marker_capture="channel_b_trig")
    d = _run(svc)
    svc.close()
    with h5py.File(d / "digitizer.h5") as f:
        assert f.attrs["device_mode_harp"] == "sim" and f.attrs["device_mode_digitizer"] == "live"
        g = f["blocks/a_01"]
        assert g["records/data_b"].shape[0] == g["records/data"].shape[0] > 0 and "data_b" not in g["markers"]
    assert _ptu_tags(d / "harp_kv_00.ptu")["UsrHHD_DeviceMode"] == "sim"
    assert validate_digitizer_file(d / "digitizer.h5") == []
    # reine Simulation: Betriebsart ebenfalls in allen Dateien
    s2 = _svc(tmp_path / "s", mock_libs, realtime=False)
    d2 = _run(s2)
    s2.close()
    man = json.loads((d2 / "session.json").read_text(encoding="utf-8"))
    assert man["device_modes"] == {"harp": "sim", "digitizer": "sim"} and man["simulated"] is True
    assert _ptu_tags(d2 / "harp_a_01.ptu")["UsrHHD_DeviceMode"] == "sim"
    with h5py.File(d2 / "digitizer.h5") as f:
        assert f.attrs["acquisition_mode"] == "sim"


def test_remote_live_start_needs_confirmation(tmp_path, mock_libs):
    svc = _svc(tmp_path, mock_libs, device_mode="live", time_scale=0.02)
    assert svc.handle("r", "10.0.0.5", False, "acquire_control", {})["ok"]
    m = svc.handle("r", "10.0.0.5", False, "start_session", {"plan": "kurz"})
    assert not m["ok"] and m["needs_confirm"] and "LIVE" in m["msg"]
    bad = svc.handle("r", "10.0.0.5", False, "start_session", {"plan": "standard", "confirm": m["confirm"]})
    assert not bad["ok"] and "passt nicht" in bad["msg"]                  # andere Aktion → Code ungültig
    m = svc.handle("r", "10.0.0.5", False, "start_session", {"plan": "kurz"})
    ok = svc.handle("r", "10.0.0.5", False, "start_session", {"plan": "kurz", "confirm": m["confirm"]})
    assert ok["ok"], ok
    assert svc.wait_idle(120) and svc.state == "fertig"
    # Triggerausgang aus der Ferne nie; kritischer Pegel nur mit Bestätigung, lokal direkt
    assert not svc.handle("r", "10.0.0.5", False, "set_device", {"device": "harp", "params": {"trigger_output": True}})["ok"]
    m = svc.handle("r", "10.0.0.5", False, "set_device", {"device": "digitizer", "params": {"trigger_level_mV": 250.0}})
    assert m["needs_confirm"]
    svc.close()
    log = [json.loads(x) for x in (tmp_path / "logs" / "commands.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(e["params"].get("confirm") in (None, "***") for e in log) and len(log) == 7


def test_missing_library_refuse_or_fallback(tmp_path, mock_libs, monkeypatch):
    monkeypatch.delenv("HHD_HH500LIB", raising=False)
    svc = _svc(tmp_path, mock_libs, device_mode="live", hh500_lib=str(tmp_path / "fehlt.so"))
    assert "Herstellerbibliothek nicht gefunden" in svc.device_error and "[harp]" in svc.device_error
    assert svc.handle("c", "127.0.0.1", True, "acquire_control", {})["ok"]
    m = svc.handle("c", "127.0.0.1", True, "start_session", {"plan": "kurz"})
    assert not m["ok"] and "Geräte nicht bereit" in m["msg"]
    assert svc.status()["devices"]["digitizer"]["open"]                    # das andere Gerät bleibt nutzbar
    svc.close()
    fb = _svc(tmp_path / "fb", mock_libs, device_mode="live", hh500_lib=str(tmp_path / "fehlt.so"), live_fallback="sim")
    st = fb.status()
    assert st["modes"] == {"harp": "sim", "digitizer": "live"} and "harp: live nicht verfügbar → sim" in st["fallback"]
    assert not fb.device_error
    d = _run(fb)
    fb.close()
    meta = json.loads((d / "server_session.json").read_text(encoding="utf-8"))
    assert meta["device_modes"] == {"harp": "sim", "digitizer": "live"} and "nicht verfügbar" in meta["fallback"]


def test_pause_between_live_runs(tmp_path, mock_libs):
    svc = _svc(tmp_path, mock_libs, device_mode="live", time_scale=0.02, live_pause_between_runs=True)
    svc.handle("c", "127.0.0.1", True, "acquire_control", {})
    assert not svc.handle("c", "127.0.0.1", True, "continue_run", {})["ok"]
    sid = svc.handle("c", "127.0.0.1", True, "start_session", {"plan": "kurz"})["session_id"]
    n = 0
    t0 = time.time()
    while svc.state not in ("fertig", "fehler") and time.time() - t0 < 120:
        if svc.waiting_continue:
            assert svc.state == "pause" and svc.status()["waiting_continue"]
            assert svc.handle("c", "127.0.0.1", True, "continue_run", {})["ok"]
            n += 1
        time.sleep(0.02)
    assert svc.state == "fertig" and n == 6
    svc.close()
    assert (svc.data_dir / sid / "session.json").exists()


def test_adq_fifo_overflow_reported(tmp_path, mock_libs, monkeypatch):
    monkeypatch.setenv("MOCK_ADQ_OVERFLOW_AFTER", "20")
    svc = _svc(tmp_path, mock_libs, device_mode="live", time_scale=0.04)
    svc.handle("c", "127.0.0.1", True, "acquire_control", {})
    svc.handle("c", "127.0.0.1", True, "start_session", {"plan": "kurz"})
    assert svc.wait_idle(60)
    assert svc.state == "fehler" and "EOVERFLOW" in svc.msg
    svc.close()


def test_harp_fifo_full_reported(tmp_path, mock_libs, monkeypatch):
    monkeypatch.setenv("MOCK_HH500_FIFOFULL_MS", "5")
    svc = _svc(tmp_path, mock_libs, device_mode="live", time_scale=0.04)
    svc.handle("c", "127.0.0.1", True, "acquire_control", {})
    svc.handle("c", "127.0.0.1", True, "start_session", {"plan": "kurz"})
    assert svc.wait_idle(60)
    assert svc.state == "fehler" and "FIFO" in svc.msg
    svc.close()


def test_example_configs_and_adq_override(tmp_path, mock_libs):
    from hhd.server.service import load_config
    for name in ("server", "server_live", "server_mixed"):
        load_config(ROOT / "config" / f"{name}.toml")                         # keine unbekannten Einträge
    assert load_config(ROOT / "config/server_mixed.toml").harp_mode == "live"
    svc = _svc(tmp_path, mock_libs, device_mode="live", adq_json_override=str(ROOT / "config/adq_override.example.json"))
    assert not svc.device_error, svc.device_error
    svc.close()
    bad = tmp_path / "bad.json"
    bad.write_text('[{"id": "ADQ_PARAMETER_ID_DATA_ACQUISITION", "channel": [{"gibt_es_nicht": 1}]}]')
    s2 = _svc(tmp_path / "b", mock_libs, device_mode="live", adq_json_override=str(bad))
    assert "[digitizer]" in s2.device_error and "gibt_es_nicht" in s2.device_error
    s2.close()
