"""Wiedergabe aus Ordner (Stufe 1, 0.15): Ordner prüfen, durchsuchen, als Wiedergabe übernehmen, abspielen,
zurücksetzen; Hochladen per Drag-and-Drop über die App."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from hhd.server.app import create_app  # noqa: E402
from hhd.server.devices import SimBackend  # noqa: E402
from hhd.server.replaysrc import ReplaySourceError, browse_dir, inspect_session_dir, upload_target  # noqa: E402
from hhd.server.service import MeasurementService, ServerConfig  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _cfg(tmp_path, **kw) -> ServerConfig:
    c = ServerConfig(data_dir=str(tmp_path / "data"), log_dir=str(tmp_path / "logs"), realtime=False,
                     analysis_inprocess=True, n_boot=10, sim_config=str(ROOT / "config/standard.yaml"), status_hz=5)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


@pytest.fixture(scope="module")
def src_session(tmp_path_factory) -> Path:
    """Vollständige simulierte Kurzsitzung (Puls-Laser) mit server_session.json wie vom Server geschrieben."""
    d = tmp_path_factory.mktemp("quelle") / "HHD-20261029-101500"
    be = SimBackend(ROOT / "config/standard.yaml", realtime=False)
    be.prepare(be.make_config("HHD-20261029-101500", 77, "kurz", "pulsed"))
    be.write_files(d)
    (d / "server_session.json").write_text(json.dumps(dict(plan="kurz", plan_label="Kurz", laser_mode="pulsed")),
                                           encoding="utf-8")
    return d


def _wait(svc, t=300):
    assert svc.wait_idle(t)


def test_inspect_ok_and_errors(src_session, tmp_path):
    r = inspect_session_dir(str(src_session))
    assert r["ok"], r
    i = r["info"]
    assert i["n_runs"] == 7 and i["laser_mode"] == "pulsed" and i["plan"] == "kurz" and i["simulated"]
    assert r["msg"].startswith("Sitzung erkannt: HHD-20261029-101500 · 7 Läufe")
    assert any("truth.h5" in w for w in r["warnings"])          # vorhanden, wird aber nie übernommen
    assert not inspect_session_dir("")["ok"]
    assert "nicht gefunden" in inspect_session_dir(str(tmp_path / "gibtsnicht"))["msg"]
    assert "vollständigen Pfad" in inspect_session_dir("relativ/ordner")["msg"]
    # unvollständige Kopien
    for drop, expect in (("session.json", "session.json fehlt"), ("digitizer.h5", "digitizer.h5 fehlt")):
        d = tmp_path / f"ohne_{drop}"
        shutil.copytree(src_session, d)
        (d / drop).unlink()
        r = inspect_session_dir(d)
        assert not r["ok"] and expect in r["msg"], r["msg"]
    d = tmp_path / "ohne_ptu"
    shutil.copytree(src_session, d)
    next(d.glob("*.ptu")).unlink()
    r = inspect_session_dir(d)
    assert not r["ok"] and "PTU-Datei fehlt (1)" in r["msg"]
    d = tmp_path / "kein_hhd"
    d.mkdir()
    (d / "session.json").write_text('{"format": "anders"}', encoding="utf-8")
    assert "kein HHD-Manifest" in inspect_session_dir(d)["msg"]


def test_browse_and_upload_target(src_session, tmp_path):
    b = browse_dir(str(src_session.parent), tmp_path)
    assert b["dirs"][0]["name"] == src_session.name and b["dirs"][0]["session"]
    assert b["parent"] and b["roots"][0]["path"] == str(tmp_path.resolve())
    assert browse_dir(str(src_session), tmp_path)["is_session"]
    with pytest.raises(ReplaySourceError):
        browse_dir(str(tmp_path / "nix"), tmp_path)
    imp = tmp_path / "_import"
    assert upload_target(imp, "HHD-1", "harp_B01.ptu") == imp / "HHD-1" / "harp_B01.ptu"
    for folder, name in (("HHD-1", "truth.h5"), ("HHD-1", "../x.ptu"), ("..", "session.json"), ("a/b", "digitizer.h5"),
                         ("HHD-1", "run.exe")):
        with pytest.raises(ReplaySourceError):
            upload_target(imp, folder, name)


def test_set_replay_dir_start_and_restore(src_session, tmp_path):
    svc = MeasurementService(_cfg(tmp_path, auto_analyze=False), tmp_path)
    assert svc.modes == {"harp": "sim", "digitizer": "sim"}
    # ohne Steuerung gesperrt
    assert not svc.handle("c", "x", True, "check_replay_dir", {"path": str(src_session)})["ok"]
    assert svc.handle("c", "x", True, "acquire_control", {})["ok"]
    r = svc.handle("c", "x", True, "check_replay_dir", {"path": str(src_session)})
    assert r["ok"] and r["check"]["info"]["n_runs"] == 7
    assert svc.handle("c", "x", True, "browse_dir", {})["browse"]["path"] == str((tmp_path / "data").resolve())
    bad = svc.handle("c", "x", True, "set_replay_dir", {"path": str(tmp_path)})
    assert not bad["ok"] and "session.json fehlt" in bad["msg"] and svc.modes["harp"] == "sim"
    r = svc.handle("c", "x", True, "set_replay_dir", {"path": str(src_session)})
    assert r["ok"], r
    st = svc.status()
    assert st["modes"] == {"harp": "replay", "digitizer": "replay"} and st["mode_labels"]["harp"] == "WIEDERGABE"
    assert st["replay"]["active"] and st["replay"]["ok"] and st["replay"]["can_restore"]
    assert st["replay"]["info"]["session_id"] == "HHD-20261029-101500"
    assert svc.display["laser_mode"] == "pulsed"
    # Start: Laufplan und Laserbetrieb der Quelle (Eingaben der App werden überstimmt)
    m = svc.handle("c", "x", True, "start_session", {"plan": "fock1", "laser_mode": "cw"})
    assert m["ok"] and "Wiedergabe von HHD-20261029-101500" in m["msg"], m
    # während der Sitzung nicht umschaltbar
    if svc._thread.is_alive():
        assert not svc.handle("c", "x", True, "clear_replay_dir", {})["ok"]
    _wait(svc)
    assert svc.state == "fertig", svc.msg
    out = tmp_path / "data" / m["session_id"]
    assert not (out / "truth.h5").exists() and (out / "digitizer.h5").is_file()
    meta = json.loads((out / "server_session.json").read_text(encoding="utf-8"))
    assert meta["plan"] == "kurz" and meta["laser_mode"] == "pulsed" and meta["acquisition_mode"] == "replay"
    assert meta["replay_source"]["session_id"] == "HHD-20261029-101500"
    # zurück zur Konfiguration
    r = svc.handle("c", "x", True, "clear_replay_dir", {})
    assert r["ok"] and svc.modes == {"harp": "sim", "digitizer": "sim"} and not svc.status()["replay"]["active"]
    svc.close()


def test_upload_folder_via_api(src_session, tmp_path):
    app = create_app(_cfg(tmp_path), tmp_path)
    with TestClient(app) as cl:
        with cl.websocket_connect("/ws") as ws:
            cid = ws.receive_json()["client"]
            files = [p for p in src_session.iterdir() if p.is_file()]
            url = "/api/replay/upload"
            # ohne Steuerung: abgelehnt
            r = cl.post(url, params=dict(folder=src_session.name, name="session.json"),
                        content=(src_session / "session.json").read_bytes(), headers={"X-HHD-Client": cid})
            assert r.status_code == 403
            ws.send_json(dict(type="command", cmd="acquire_control", params={}, id=1))
            for _ in range(50):
                msg = ws.receive_json()
                if msg.get("type") == "ack":
                    assert msg["ok"], msg
                    break
            skipped = []
            for p in files:
                r = cl.post(url, params=dict(folder=src_session.name, name=p.name), content=p.read_bytes(),
                            headers={"X-HHD-Client": cid})
                if p.name == "truth.h5":
                    assert r.status_code == 400
                    skipped.append(p.name)
                else:
                    assert r.status_code == 200, r.text
            dest = tmp_path / "data" / "_import" / src_session.name
            assert skipped == ["truth.h5"] and not (dest / "truth.h5").exists()
            assert inspect_session_dir(dest)["ok"]
            assert r.json()["path"] == str(dest.resolve())
