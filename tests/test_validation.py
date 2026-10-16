"""Validierung der erzeugten Wellenformen gegen den Sollzustand (u. a. W(0,0))."""
import numpy as np
import pytest

from hhd.sim.check import RunReader, check_run, phase_fit
from hhd.sim.config import BlockCfg, SimConfig
from hhd.sim.engine import run


@pytest.fixture(scope="module")
def std_check(std_run):
    return check_run(std_run[1], n_boot=60)


def test_vacuum_normalisation(std_run, std_check):
    cfg = std_run[0].cfg
    vn = std_check["vacuum_norm"]
    assert vn["offset"] == pytest.approx(cfg.digitizer.adc_offset, abs=0.1)
    assert vn["scale"] == pytest.approx(cfg.digitizer.lsb_per_unit * np.sqrt(1 + 1 / cfg.clearance), rel=0.04)


def test_heralded_w00_negative_and_consistent(std_check):
    b = std_check["blocks"]["heralded"]
    assert b["n"] > 2500
    assert abs(b["W00_pull"]) < 4
    assert b["W00"] + 4 * b["W00_err"] < 0                       # Nichtklassikalität sichtbar
    assert b["var_x"] == pytest.approx(b["expected"]["var_x"], abs=4 * b["var_err"])


@pytest.mark.parametrize("bid", ["kv_end", "kh", "random", "heralded/random"])
def test_references_are_vacuum(std_check, bid):
    b = std_check["blocks"][bid]
    assert b["W00"] == pytest.approx(1 / np.pi, abs=max(4 * b["W00_err"], 0.01))
    assert b["var_x"] == pytest.approx(0.5, abs=4 * b["var_err"])


def test_per_record_quadrature_equals_truth(std_run):
    """Projektion der Records auf die wahre Mode = wahre Quadratur + elektronisches Rauschen."""
    res, d = std_run
    rr = RunReader(d)
    try:
        x = rr.quadratures("heralded")
        g = rr.tru["blocks/heralded"]
        kind, idx = g["records/kind"][:], g["records/source_index"][:]
        pair = g["heralds/pair"][:]
        mk, ms, mx = g["modes/kind"][:], g["modes/source_index"][:], g["modes/x"][:]
        lut = dict(zip(ms[mk == 0], mx[mk == 0]))
        sel = [(j, lut[pair[idx[j]]]) for j in np.flatnonzero(kind == 0) if pair[idx[j]] >= 0 and pair[idx[j]] in lut]
        j, xt = map(np.array, zip(*sel))
        S = res.cfg.clearance
        resid = x[j] * np.sqrt(1 + 1 / S) - xt
        # Restvarianz: elektronisches Rauschen 0,5/S plus Beiträge benachbarter Moden
        assert resid.var() == pytest.approx(0.5 / S, rel=0.25)
        assert np.corrcoef(x[j], xt)[0, 1] > 0.98
    finally:
        rr.close()


def test_known_phase_coherent(phase_run):
    r = check_run(phase_run[1], n_boot=30)
    for bid in ("coh_ramp", "coh_steps"):
        b = r["blocks"][bid]
        pf, e = b["phase_fit"], b["expected"]
        assert pf["A"] == pytest.approx(e["A"], abs=4 * pf["A_err"] + 0.01)
        assert pf["phi"] == pytest.approx(e["phi"], abs=4 * pf["A_err"] / e["A"] + 0.01)
        assert pf["residual_var"] == pytest.approx(0.5, rel=0.1)
    ramp = r["blocks"]["coh_ramp"]
    assert abs(ramp["W00_pull"]) < 4                                # Rampe = gleichverteilte Phase
    assert "W00" not in r["blocks"]["coh_steps"]["expected"]        # Stufen: nicht phasengemittelt


def test_extension_states(tmp_path):
    cfg = SimConfig()
    cfg.session.seed = 9
    cfg.blocks = [BlockCfg(id="kv", kind="vacuum", duration_s=0.5, periodic_rate=4000),
                  BlockCfg(id="fock2", kind="state", duration_s=1.0, state_rate=2000, state={"type": "fock", "n": 2}),
                  BlockCfg(id="cat", kind="state", duration_s=1.0, state_rate=2000, state={"type": "cat", "alpha": 1.5},
                           phase={"mode": "ramp", "ramp_Hz": 20.0})]
    run(cfg, tmp_path)
    r = check_run(tmp_path, n_boot=40)
    for bid in ("fock2", "cat"):
        b = r["blocks"][bid]
        assert abs(b["W00_pull"]) < 4, (bid, b)


def test_phase_fit_unit():
    rng = np.random.default_rng(0)
    th = rng.uniform(0, 2 * np.pi, 5000)
    x = 0.8 * np.cos(th - 1.0) + rng.normal(0, 0.7, th.size)
    pf = phase_fit(x, th)
    assert pf["A"] == pytest.approx(0.8, abs=4 * pf["A_err"]) and pf["phi"] == pytest.approx(1.0, abs=0.1)


def test_crosstalk_detected_by_kh(tmp_path):
    """Fehlerbild R10: Übersprechen verfälscht K-H deutlich – die Pflichtkontrolle erkennt es."""
    cfg = SimConfig.from_dict({"digitizer": {"crosstalk_amp": 0.3}})
    cfg.session.seed = 2
    cfg.blocks = [BlockCfg(id="kv", kind="vacuum", duration_s=0.5, periodic_rate=4000),
                  BlockCfg(id="kh", kind="herald_blocked", duration_s=1.0)]
    run(cfg, tmp_path)
    b = check_run(tmp_path, n_boot=30)["blocks"]["kh"]
    assert abs(b["W00_pull"]) > 4 or abs(b["mean_x"]) > 5 * np.sqrt(b["var_x"] / b["n"])
