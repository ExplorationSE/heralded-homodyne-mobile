"""Laserbetrieb der Pumpe (0.15): CW (bisher, Poisson, double_exponential) gegen Puls (Paare nur in den Pumppulsen,
pulssynchrone Zufallstrigger, gaußförmige Mode) – Konfiguration, Simulator, Geräteattribute, Auswertung, Server."""
from __future__ import annotations

import json
from pathlib import Path

import h5py
import numpy as np
import pytest

from hhd.modes.extract import laser_info, resolve_mode_shape
from hhd.sim.config import BlockCfg, ConfigError, SimConfig
from hhd.sim.engine import pulse_origin, run, simulate

ROOT = Path(__file__).resolve().parents[1]


def _small(laser: str) -> SimConfig:
    cfg = SimConfig.from_dict({"source": {"laser_mode": laser}})
    cfg.blocks = [BlockCfg(id="kv", kind="vacuum", duration_s=0.05, periodic_rate=2000),
                  BlockCfg(id="h", kind="heralded", duration_s=0.2, random_rate=200),
                  BlockCfg(id="r", kind="random", duration_s=0.1, random_rate=2000)]
    return cfg


def _off_grid(t, cfg):
    """Abstand zum nächsten Pumppuls in Einheiten des Pulsabstands."""
    u = (np.asarray(t) - pulse_origin(cfg)) * cfg.source.pulse_rate_Hz
    return np.abs(u - np.rint(u))


def test_config_laser_mode():
    assert SimConfig().source.laser_mode == "cw" and SimConfig().source.mode_shape == "double_exponential"
    p = SimConfig.from_dict({"source": {"laser_mode": "pulsed"}})
    assert p.source.mode_shape == "gauss"                                     # Puls ohne Angabe: gauß
    q = SimConfig.from_dict({"source": {"laser_mode": "pulsed", "mode_shape": "double_exponential"}})
    assert q.source.mode_shape == "double_exponential"                        # ausdrücklich angegeben bleibt
    p.validate()
    with pytest.raises(ConfigError):
        SimConfig.from_dict({"source": {"laser_mode": "qcw"}}).validate()
    with pytest.raises(ConfigError):
        SimConfig().set_laser_mode("x")
    c = SimConfig().set_laser_mode("pulsed")
    assert (c.source.laser_mode, c.source.mode_shape) == ("pulsed", "gauss")
    c.set_laser_mode("cw")
    assert (c.source.laser_mode, c.source.mode_shape) == ("cw", "double_exponential")
    w = SimConfig.from_dict({"source": {"laser_mode": "pulsed", "pulse_rate_Hz": 1e4}}).validate()
    assert any("Puls" in x for x in w)                                        # Mehrpaaranteil je Puls


def test_pulsed_sim_on_pulse_grid(tmp_path):
    cfg = _small("pulsed")
    res = run(cfg, tmp_path / "p")
    h = [b for b in res.blocks if b.cfg.id == "h"][0]
    d = cfg.digitizer
    m = h.truth["modes"]
    t_pair = np.asarray(m["centre_true"])[np.asarray(m["kind"]) <= 1] - d.d_S
    assert len(t_pair) > 100 and _off_grid(t_pair, cfg).max() < 1e-6        # Paare nur in den Pumppulsen
    t_r = np.asarray(h.truth["random"]["t_true"])
    assert len(t_r) > 10 and _off_grid(t_r + d.d_RT - d.d_T, cfg).max() < 1e-6   # pulssynchrone Zufallsfenster
    r = [b for b in res.blocks if b.cfg.id == "r"][0]
    assert len(r.truth["random"]["t_true"]) == pytest.approx(2000 * 0.1, rel=0.25)
    with h5py.File(tmp_path / "p" / "digitizer.h5") as f:
        a = dict(f["device"].attrs)
    assert a["laser_mode"] == "pulsed" and a["mode_shape"] == "gauss" and a["pulse_rate_Hz"] == 1e6
    # CW (bisher): Zeiten nicht auf einem Raster
    c = simulate(_small("cw"))
    hc = [b for b in c.blocks if b.cfg.id == "h"][0]
    tc = np.asarray(hc.truth["modes"]["centre_true"]) - d.d_S
    assert np.median(_off_grid(tc, c.cfg)) > 0.1
    assert c.device_attrs["laser_mode"] == "cw" and "pulse_rate_Hz" not in c.device_attrs


def test_analysis_mode_shape_auto():
    assert resolve_mode_shape("auto", {}) == "double_exponential"            # alte Sitzungen
    assert resolve_mode_shape("auto", {"mode_shape": "gauss"}) == "gauss"
    assert resolve_mode_shape("auto", {"mode_shape": b"gauss"}) == "gauss"
    assert resolve_mode_shape("double_exponential", {"mode_shape": "gauss"}) == "double_exponential"
    assert laser_info({}) == dict(mode="cw", mode_shape="double_exponential")
    assert laser_info({"laser_mode": "pulsed", "mode_shape": "gauss", "pulse_rate_Hz": 1e6}) == \
        dict(mode="pulsed", mode_shape="gauss", pulse_rate_Hz=1e6)


def test_server_pulsed_session(tmp_path):
    pytest.importorskip("fastapi")
    from hhd.server.service import MeasurementService, ServerConfig
    c = ServerConfig(data_dir=str(tmp_path / "data"), log_dir=str(tmp_path / "logs"), realtime=False,
                     analysis_inprocess=True, n_boot=10, sim_config=str(ROOT / "config/standard.yaml"), status_hz=5)
    svc = MeasurementService(c, tmp_path)
    assert svc.status()["display"]["laser_mode"] == "cw"
    assert svc.handle("c", "x", True, "acquire_control", {})["ok"]
    assert not svc.handle("c", "x", True, "set_display", {"laser_mode": "qcw"})["ok"]
    assert svc.handle("c", "x", True, "set_display", {"laser_mode": "pulsed"})["ok"]
    assert svc.display["laser_mode"] == "pulsed"
    m = svc.handle("c", "x", True, "start_session", {"plan": "kurz"})
    assert m["ok"], m
    assert svc.wait_idle(600) and svc.state == "fertig", svc.msg
    d = tmp_path / "data" / m["session_id"]
    assert json.loads((d / "server_session.json").read_text(encoding="utf-8"))["laser_mode"] == "pulsed"
    r = json.loads((d / "analysis" / "result.json").read_text(encoding="utf-8"))
    assert r["laser"]["mode"] == "pulsed" and r["mode"]["shape"] == "gauss" and r["params"]["mode_shape"] == "auto"
    sm = svc.summary(m["session_id"])
    assert sm["laser"]["mode"] == "pulsed" and sm["mode"]["shape"] == "gauss"
    A, B = sm["groups"]["A"], sm["groups"]["B"]
    assert A["W00"] < 0 < B["W00"] and A["n"] > 500
    s = [x for x in svc.sessions() if x["id"] == m["session_id"]][0]
    assert s["laser_mode"] == "pulsed"
    assert svc.live.mode_shape == "gauss"
    # Startparameter überschreibt die Anzeige: wieder CW
    m2 = svc.handle("c", "x", True, "start_session", {"plan": "kurz", "laser_mode": "cw"})
    assert m2["ok"] and svc.wait_idle(600)
    r2 = json.loads((tmp_path / "data" / m2["session_id"] / "analysis" / "result.json").read_text(encoding="utf-8"))
    assert r2["laser"]["mode"] == "cw" and r2["mode"]["shape"] == "double_exponential"
