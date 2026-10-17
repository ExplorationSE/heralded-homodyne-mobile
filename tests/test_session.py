"""ED-12: getrennte Messläufe, Sitzung/Kampagne, gemeinsame Kalibrierung, Drift und Gegenmaßnahmen."""
import json

import h5py
import numpy as np
import pytest
from scipy import signal

from hhd.io import ptu as P
from hhd.sim.check import check_run
from hhd.sim.cli import validate_run
from hhd.sim.config import BlockCfg, ConfigError, SimConfig
from hhd.sim.engine import run, run_campaign


def _sess(tmp_path_factory, name, seed, patch=None):
    d = tmp_path_factory.mktemp(name)
    cfg = SimConfig.from_dict(patch or {})
    cfg.session.seed = seed
    return run(cfg, d), d


@pytest.fixture(scope="module")
def sep_run(tmp_path_factory):
    return _sess(tmp_path_factory, "sep", 7)


@pytest.fixture(scope="module")
def sep_check(sep_run):
    return check_run(sep_run[1], n_boot=40)


@pytest.fixture(scope="module")
def lo_run(tmp_path_factory):
    return _sess(tmp_path_factory, "lo", 21, {"drift": {"lo_rel_per_s": 0.08}})


@pytest.fixture(scope="module")
def eta_run(tmp_path_factory):
    return _sess(tmp_path_factory, "eta", 22, {"drift": {"eta_rel_per_s": -0.06}})


# ------------------------------------------------------------------ Konfiguration
def test_defaults_follow_decisions():
    c = SimConfig()
    assert c.harp.model == "HydraHarp 500 S" and c.harp.n_channels == 4
    assert c.digitizer.form_factor == "PCIe" and c.digitizer.clock_option == "S2G5" and c.fs == 2.5e9
    assert c.digitizer.bandwidth_option == "BW1G0" and c.bandwidth == 1e9
    assert [b.kind for b in c.blocks] == ["vacuum", "herald_blocked", "heralded", "free_running", "heralded", "vacuum"]
    assert all(b.random_rate == 0 for b in c.blocks if b.kind == "heralded")    # kein Einstreuen (Option)
    assert c.run_ids() == [f"S001-R{i:02d}" for i in range(1, 7)]


@pytest.mark.parametrize("patch, msg", [
    ({"digitizer": {"bandwidth_option": "BW3G0"}}, "BW1G0"),
    ({"drift": {"lo_rel_per_s": -0.2}}, "≤ 5 %"),
    ({"blocks": [{"id": "a", "run_id": "X"}, {"id": "b", "run_id": "X"}]}, "run_id doppelt"),
    ({"blocks": [{"id": "a", "lo_scale": 0}]}, "lo_scale"),
    ({"session": {"id": ""}}, "nicht leer"),
])
def test_session_config_errors(patch, msg):
    with pytest.raises(ConfigError, match=msg):
        SimConfig.from_dict(patch).validate()


# ------------------------------------------------------------------ Struktur und IDs
def test_ids_and_shared_calibration(sep_run):
    res, d = sep_run
    assert validate_run(d) == []
    man = json.loads((d / "session.json").read_text(encoding="utf-8"))
    assert man["format"] == "hhd-session" and man["session_id"] == "S001" and man["campaign_id"] == "K-SIM"
    cal = man["calibration"]["calibration_id"]
    assert cal == "K-SIM-S001-cal" and man["calibration"]["vacuum_runs"] == ["S001-R01", "S001-R06"]
    assert [r["comparison_role"] for r in man["runs"]] == ["control", "control", "heralded", "unheralded", "heralded", "control"]
    with h5py.File(d / "digitizer.h5") as f:
        assert f.attrs["session_id"] == "S001" and f.attrs["calibration_id"] == cal
        assert f.attrs["schema_version"] == "1.2"
        for r in man["runs"]:
            g = f[f"blocks/{r['id']}"]
            assert g.attrs["run_id"] == r["run_id"] and g.attrs["calibration_id"] == cal
            assert g.attrs["t_start_s"] == pytest.approx(r["t_start_s"])
            tags = P.read_ptu(d / r["ptu_file"]).tags
            assert (tags["UsrHHD_RunId"], tags["UsrHHD_SessionId"], tags["UsrHHD_CampaignId"],
                    tags["UsrHHD_CalibrationId"]) == (r["run_id"], "S001", "K-SIM", cal)
        assert f["device"].attrs["analog_bandwidth_Hz"] == 1e9


def test_validate_detects_foreign_run(sep_run, tmp_path):
    _, d = sep_run
    import shutil
    dd = tmp_path / "kopie"
    shutil.copytree(d, dd)
    man = json.loads((dd / "session.json").read_text(encoding="utf-8"))
    man["runs"][2]["run_id"] = "S999-R03"
    (dd / "session.json").write_text(json.dumps(man), encoding="utf-8")
    assert any("Lauf-/Sitzungs-ID" in e for e in validate_run(dd))


def test_separate_runs_trigger_content(sep_run):
    res, _ = sep_run
    for br in res.blocks:
        k = br.digitizer.rec_kind
        if br.cfg.kind == "heralded":
            assert np.all(k == 0) and len(br.truth["random"]["t_true"]) == 0
        elif br.cfg.kind == "free_running":
            assert np.all(k == 2) and len(br.truth["heralds"]["t_true"]) > 0     # Heralds laufen mit, triggern aber nicht
    for b in res.blocks:                                                         # LO-Monitor an Kanal B
        lm = res.cfg.lo_monitor
        assert b.monitor is None and b.digitizer.data_b.shape == (len(b.digitizer.timestamp), 200)
        assert (b.digitizer.data_b.mean() - lm.dark_lsb) / lm.lsb_nominal == pytest.approx(1.0, abs=0.002)


# ------------------------------------------------------------------ Vergleich A/B ohne Drift
def test_comparison_without_drift(sep_check):
    c = sep_check["comparison"]
    a, b = c["heralded"], c["unheralded"]
    assert a["runs"] == ["heralded_1", "heralded_2"] and b["runs"] == ["unheralded"]
    assert abs(a["W00_pull"]) < 4 and a["W00"] + 4 * a["W00_err"] < 0
    assert b["W00"] == pytest.approx(1 / np.pi, abs=4 * b["W00_err"] + 0.01)
    assert c["heralded_minus_unheralded"]["W00"]["z"] < -10
    dr = c["drift"]
    assert abs(dr["aba"]["W00"]["z"]) < 3 and abs(dr["aba"]["var_x"]["z"]) < 3 and abs(dr["vacuum"]["z"]) < 3
    assert dr["flags"] == []


# ------------------------------------------------------------------ Drift und Gegenmaßnahmen
def test_lo_drift_detected_and_corrected(lo_run):
    res, d = lo_run
    S, lsb = res.cfg.clearance, res.cfg.digitizer.lsb_per_unit
    pooled = check_run(d, n_boot=20, norm_mode="pooled")
    dr = pooled["comparison"]["drift"]
    assert dr["vacuum"]["z"] > 4 and dr["aba"]["var_x"]["z"] > 4 and len(dr["flags"]) == 3
    assert dr["lo_monitor"]["rel_change"] == pytest.approx(0.08 * 10.0, rel=0.05)
    for mode in ("interpolated", "monitor"):
        r = check_run(d, n_boot=20, norm_mode=mode)
        assert abs(r["comparison"]["drift"]["aba"]["var_x"]["z"]) < 3, mode
        for bid in ("heralded_1", "heralded_2", "unheralded"):
            b = r["blocks"][bid]
            assert abs(b["var_x"] - b["expected"]["var_x"]) < 3.5 * b["var_err"], (mode, bid)
            lo = [x for x in res.blocks if x.cfg.id == bid][0].expected["lo_scale"]
            assert r["scales"][bid] == pytest.approx(lsb * np.sqrt(lo + 1 / S), rel=0.025), (mode, bid)


def test_eta_drift_only_visible_in_aba(eta_run):
    r = check_run(eta_run[1], n_boot=30)
    dr = r["comparison"]["drift"]
    assert abs(dr["vacuum"]["z"]) < 3                     # K-V sieht eine Effizienzdrift nicht
    assert dr["aba"]["W00"]["z"] > 4                      # A-B-A schon
    assert any("A-B-A" in f for f in dr["flags"])
    a = r["comparison"]["heralded"]
    assert abs(a["W00_pull"]) < 4                         # Mittel der A-Läufe = Zustand zur Zeit von B (lineare Drift)


# ------------------------------------------------------------------ Laufart free_running, Bandbreite, Kampagne
def test_free_running_run(tmp_path):
    cfg = SimConfig.from_dict({"blocks": [{"id": "kv", "kind": "vacuum", "duration_s": 0.5, "periodic_rate": 4000},
                                          {"id": "frei", "kind": "free_running", "duration_s": 1.0, "periodic_rate": 2000}]})
    res = run(cfg, tmp_path)
    br = res.blocks[1]
    assert br.dataset_type == "free_running" and br.attrs["comparison_role"] == "unheralded"
    assert np.all(br.digitizer.rec_kind == 2) and len(br.truth["modes"]["x"]) > 0
    assert validate_run(tmp_path) == []
    b = check_run(tmp_path, n_boot=30)["blocks"]["frei"]
    assert b["W00"] == pytest.approx(b["expected"]["W00"], abs=4 * b["W00_err"] + 0.01)
    assert b["var_x"] == pytest.approx(b["expected"]["var_x"], abs=4 * b["var_err"])


def test_bandwidth_filter(tmp_path):
    out = {}
    for bw in ("BW1G0", "BW2G5"):
        cfg = SimConfig.from_dict({"digitizer": {"bandwidth_option": bw},
                                   "blocks": [{"id": "kv", "kind": "vacuum", "duration_s": 0.3, "periodic_rate": 2000}]})
        res = run(cfg, tmp_path / bw)
        y = res.blocks[0].digitizer.data.astype(float)
        out[bw] = (y[:, 50:].var(), check_run(tmp_path / bw, n_boot=5)["vacuum_norm"]["scale"])
    b, a = signal.butter(1, 1e9 / 1.25e9)
    gain = np.sum(signal.lfilter(b, a, np.r_[1.0, np.zeros(200)]) ** 2)
    assert out["BW1G0"][0] / out["BW2G5"][0] == pytest.approx(gain, rel=0.03)      # Rauschen je Sample gefärbt
    assert out["BW1G0"][1] == pytest.approx(out["BW2G5"][1], rel=0.01)            # Mode unbeeinflusst


def test_campaign(tmp_path):
    cfg = SimConfig.from_dict({"session": {"id": "T", "campaign": "K-TEST"},
                               "blocks": [{"id": "kv", "kind": "vacuum", "duration_s": 0.2, "periodic_rate": 2000},
                                          {"id": "h", "kind": "heralded", "duration_s": 0.3}]})
    man = run_campaign(cfg, tmp_path, n_sessions=2)
    assert json.loads((tmp_path / "campaign.json").read_text(encoding="utf-8")) == man
    ids = [s["session_id"] for s in man["sessions"]]
    assert ids == ["T-01", "T-02"] and [s["seed"] for s in man["sessions"]] == [cfg.session.seed, cfg.session.seed + 1]
    assert len({s["calibration_id"] for s in man["sessions"]}) == 2
    for s in man["sessions"]:
        assert validate_run(tmp_path / s["dir"]) == []
        assert s["runs"] == [f"{s['session_id']}-R01", f"{s['session_id']}-R02"]
