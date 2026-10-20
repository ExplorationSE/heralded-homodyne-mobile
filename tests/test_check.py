"""Geräteprüfung ohne Messung (``hhd.server.check``, ``hhd check-devices``, GET /api/devices/check) gegen die
C-Mock-Bibliotheken; Fehlerfälle Bibliothek fehlt / kein Gerät / Gerät belegt; Laufwerk-D:-Rückfall; Startskripte."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from hhd.server import check as CK  # noqa: E402
from hhd.server.app import create_app  # noqa: E402
from hhd.server.service import MeasurementService, ServerConfig  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_lib_env(monkeypatch):
    """Bibliothekspfade nur aus der Testkonfiguration (Umgebungsvariablen sind Rückfall-Kandidaten)."""
    for k in ("HHD_HH500LIB", "HHD_ADQAPI"):
        monkeypatch.delenv(k, raising=False)


def _cfg(tmp_path, mock_libs, **kw) -> ServerConfig:
    c = ServerConfig(data_dir=str(tmp_path / "data"), log_dir=str(tmp_path / "logs"), analysis_inprocess=True,
                     n_boot=10, sim_config=str(ROOT / "config/standard.yaml"), time_scale=0.08, auto_analyze=False,
                     hh500_lib=str(mock_libs["hh500"]), adq_lib=str(mock_libs["adq"]), device_mode="live",
                     adq_trace_dir=str(tmp_path / "trace"))
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _toml(tmp_path, mock_libs, extra: str = "", harp_lib=None) -> Path:
    p = tmp_path / "live.toml"
    hl = mock_libs["hh500"] if harp_lib is None else harp_lib
    p.write_text(f'''[server]
data_dir = "{(tmp_path / 'data').as_posix()}"
log_dir = "{(tmp_path / 'logs').as_posix()}"
[devices]
device_mode = "live"
{extra}
[live]
hh500_lib = "{Path(hl).as_posix()}"
adq_lib = "{Path(mock_libs['adq']).as_posix()}"
adq_trace_dir = "{(tmp_path / 'trace').as_posix()}"
''', encoding="utf-8")
    return p


def test_check_ok(tmp_path, mock_libs):
    s = CK.check_devices(_cfg(tmp_path, mock_libs), tmp_path)
    assert s["ok"] and s["n_checked"] == 2 and s["schema"].startswith("hhd-geraetepruefung/")
    h, d = s["devices"]
    assert h["kind"] == "harp" and h["category"] == "ok" and h["device_found"] and "MOCK" in h["model"]
    assert h["serial"] == "1050042" and h["details"]["n_inputs"] == 4 and h["details"]["sync_input"]
    assert h["library_found"] and h["library_version"]
    assert d["kind"] == "digitizer" and d["ok"] and d["serial"] == "SPD-MOCK1" and d["model"].startswith("ADQ32")
    assert d["details"]["n_channels"] == 2 and d["details"]["sampling_rate_Hz"] == pytest.approx(2.5e9)
    assert d["library_version"].startswith("MOCK-") and "FWDAQ" in d["firmware"]
    txt = CK.format_text(s)
    assert "HydraHarp 500" in txt and "ADQ32" in txt and "2.50 GS/s" in txt and "in Ordnung" in txt


def test_check_mixed_only_live(tmp_path, mock_libs):
    s = CK.check_devices(_cfg(tmp_path, mock_libs, digitizer_mode="sim"), tmp_path)
    assert s["n_checked"] == 1 and s["ok"]
    assert s["devices"][1]["category"] == "nicht_geprueft" and not s["devices"][1]["checked"]
    s = CK.check_devices(_cfg(tmp_path, mock_libs, digitizer_mode="sim"), tmp_path, all_devices=True)
    assert s["n_checked"] == 2 and s["devices"][1]["ok"]


def test_check_lib_missing(tmp_path, mock_libs):
    s = CK.check_devices(_cfg(tmp_path, mock_libs, hh500_lib=str(tmp_path / "gibtsnicht.so")), tmp_path)
    h = s["devices"][0]
    assert not s["ok"] and h["category"] == "treiber_fehlt" and not h["library_found"]
    assert "HHD_HH500LIB" in h["hint"] and "hh500_lib" in h["hint"]
    assert s["devices"][1]["ok"]                     # ADQ trotzdem geprüft
    assert "nicht gefunden" in CK.format_text(s)


@pytest.mark.parametrize("env,kind", [("MOCK_HH500_NODEV", 0), ("MOCK_ADQ_NODEV", 1)])
def test_check_no_device(tmp_path, mock_libs, monkeypatch, env, kind):
    monkeypatch.setenv(env, "1")
    s = CK.check_devices(_cfg(tmp_path, mock_libs), tmp_path)
    r = s["devices"][kind]
    assert not s["ok"] and r["category"] == "kein_geraet" and not r["device_found"]
    assert ("USB" if kind == 0 else "PCIe") in r["hint"]
    assert s["devices"][1 - kind]["ok"]


@pytest.mark.parametrize("env,kind", [("MOCK_HH500_BUSY", 0), ("MOCK_ADQ_BUSY", 1)])
def test_check_busy(tmp_path, mock_libs, monkeypatch, env, kind):
    monkeypatch.setenv(env, "1")
    s = CK.check_devices(_cfg(tmp_path, mock_libs), tmp_path)
    r = s["devices"][kind]
    assert not s["ok"] and r["category"] == "belegt", r
    assert "schließen" in r["hint"]


def test_cli_exit_codes_and_json(tmp_path, mock_libs, monkeypatch, capsys):
    p = _toml(tmp_path, mock_libs)
    assert CK.main(["--config", str(p)]) == 0
    out = capsys.readouterr().out
    assert "Geräteprüfung" in out and "in Ordnung" in out
    j = json.loads((tmp_path / "logs/geraetepruefung.json").read_text(encoding="utf-8"))
    assert j["ok"] and j["n_checked"] == 2
    monkeypatch.setenv("MOCK_ADQ_NODEV", "1")
    assert CK.main(["--config", str(p), "--json", str(tmp_path / "x.json")]) == 1
    assert not json.loads((tmp_path / "x.json").read_text(encoding="utf-8"))["ok"]
    assert CK.main(["--config", str(tmp_path / "fehlt.toml")]) == 2
    p2 = _toml(tmp_path, mock_libs).with_name("sim.toml")
    p2.write_text(_toml(tmp_path, mock_libs).read_text(encoding="utf-8").replace('"live"', '"sim"'), encoding="utf-8")
    assert CK.main(["--config", str(p2)]) == 3


def test_cli_lib_missing_exit(tmp_path, mock_libs, capsys):
    p = _toml(tmp_path, mock_libs, harp_lib=tmp_path / "nix.so")
    assert CK.main(["--config", str(p)]) == 1
    assert "HHD_HH500LIB" in capsys.readouterr().out


def test_hhd_cli_routing(tmp_path, mock_libs, capsys):
    from hhd import cli
    p = _toml(tmp_path, mock_libs)
    rc = cli.main(["check-devices", "--config", str(p)])
    assert rc == 0 and "ADQ32" in capsys.readouterr().out


def test_drive_fallback(tmp_path):
    c = ServerConfig(data_dir="Q:/hhd_data", log_dir="Q:/hhd_logs", adq_trace_dir="Q:/hhd_logs/adq_trace")
    if sys.platform == "win32" and Path("Q:/").exists():
        pytest.skip("Laufwerk Q: vorhanden")
    msgs = CK.resolve_paths(c, tmp_path)
    assert c.data_dir == "hhd_data" and c.log_dir == "logs" and c.adq_trace_dir == "logs/adq_trace"
    assert len(msgs) == 3 and all("nicht vorhanden" in m for m in msgs)
    c = ServerConfig(data_dir=str(tmp_path / "d"), log_dir="logs")
    assert CK.resolve_paths(c, tmp_path) == [] and c.data_dir == str(tmp_path / "d")


def test_service_check_reopens_devices(tmp_path, mock_libs):
    svc = MeasurementService(_cfg(tmp_path, mock_libs), tmp_path)
    try:
        assert svc.harp.is_open and svc.dig.is_open        # Server hält die Live-Geräte offen
        r = svc.check_devices()
        assert r["ok"] and r["server_devices_reopened"] == ["harp", "digitizer"]
        assert svc.harp.is_open and svc.dig.is_open and not svc.device_errors
        assert (tmp_path / "logs/geraetepruefung.json").exists()
        st = svc.status()["device_check"]
        assert st["last"]["ok"] and not st["running"]
    finally:
        svc.close()


def test_api_check_and_lock(tmp_path, mock_libs):
    cfg = _cfg(tmp_path, mock_libs, time_scale=1.0)
    app = create_app(cfg, tmp_path)
    svc = app.state.svc
    with TestClient(app) as cl:
        r = cl.get("/api/devices/check")
        assert r.status_code == 200 and r.json()["ok"] and r.json()["n_checked"] == 2
        assert cl.get("/api/devices/check?alle=true").json()["all_devices"]
        assert svc.handle("c", "127.0.0.1", True, "acquire_control", {})["ok"]
        assert svc.handle("c", "127.0.0.1", True, "start_session", {"plan": "kurz"})["ok"]
        try:
            r = cl.get("/api/devices/check")
            assert r.status_code == 409 and "Sitzung" in r.json()["detail"]
        finally:
            svc.handle("c", "127.0.0.1", True, "stop_session", {})
            assert svc.wait_idle(120)
        assert cl.get("/api/devices/check").status_code == 200
        log = (tmp_path / "logs").glob("*.jsonl")
        assert any("check_devices" in f.read_text(encoding="utf-8") for f in log)
