"""Quellmodell für Lauf B (0.15): spdc / squeezed / test, Soll der Quetschung, Mandel Q, Phasenauswertung B, App."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from hhd.sim.config import SimConfig
from hhd.sim.states import (PRESET_SOURCE, Squeezed, background_rho, make_state, mandel_q, source_model,
                            source_short, squeezing_dB)

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "config" / "standard.yaml"


def test_preset_source_mapping():
    for p in ("fock1", "fock2", "fock3", "fock01"):
        assert source_model(p)["model"] == "spdc" and source_model(p)["unheralded"] is None
    for p in ("cat_even_1", "cat_odd_1", "cat_even_2", "cat_odd_2", "kitten_3dB", "squeezed_3dB", "squeezed_6dB"):
        assert source_model(p)["model"] == "squeezed"
    for p in ("coherent_1", "thermal_0.5"):
        assert source_model(p)["model"] == "test"
    assert set(PRESET_SOURCE.values()) == {"spdc", "squeezed", "test"}


def test_cat_subtraction_parameters():
    s = source_model("cat_odd_1")
    assert s["n_subtracted"] == 1 and s["phi"] == pytest.approx(np.pi / 2)
    assert s["dB"] == pytest.approx(2.71, abs=0.02) and s["fidelity"] > 0.99
    s2 = source_model("cat_even_2")
    assert s2["n_subtracted"] == 2 and s2["dB"] == pytest.approx(6.20, abs=0.05) and s2["fidelity"] > 0.88
    assert source_model("squeezed_6dB")["n_subtracted"] == 0 and source_model("squeezed_6dB")["dB"] == pytest.approx(6.0)


def test_explicit_model_and_squeeze_override():
    s = source_model("fock1", "squeezed", 4.0)
    assert s["model"] == "squeezed" and s["dB"] == pytest.approx(4.0) and not s["auto"]
    assert source_model("cat_even_2", "spdc")["unheralded"] is None
    with pytest.raises(ValueError):
        source_model("fock1", "laser")
    c = SimConfig(); c.source.model = "quatsch"
    with pytest.raises(ValueError):
        c.validate()
    c = SimConfig(); c.source.squeeze_dB = 25.0
    with pytest.raises(ValueError):
        c.validate()


def test_spdc_default_unchanged():
    from hhd.sim.engine import background_state
    c = SimConfig()
    assert c.source.model == "auto" and background_state(c) is None
    assert background_rho({"preset": "fock1", "source": source_model("fock1")}) is None
    assert background_rho(None) is None


def test_squeezed_6dB_detector_soll():
    """Lauf B bei 6 dB, Detektor η·η_el = 0,7·0,9693 = 0,6785: min Var = 0,2459 → −3,08 dB."""
    st = Squeezed(r=Squeezed.r_from_dB(6.0)); st.n_fock = 60
    vmin, axis = st.var_min(0.6785)
    assert vmin == pytest.approx(0.6785 * 0.5 * 10 ** -0.6 + 0.5 * (1 - 0.6785), rel=1e-3)
    assert squeezing_dB(vmin) == pytest.approx(-3.08, abs=0.01)
    assert axis == pytest.approx(0.0, abs=1e-6) or axis == pytest.approx(np.pi, abs=1e-6)
    e = st.expected(0.7)
    assert e["mandel_Q"] > 0                       # gequetschtes Vakuum: super-poissonsch


def test_mandel_q_values_and_loss_scaling():
    f1 = make_state("fock2")
    assert f1.expected(1.0)["mandel_Q"] == pytest.approx(-1.0, abs=1e-9)
    for eta in (0.3, 0.7):
        assert f1.expected(eta)["mandel_Q"] == pytest.approx(-eta, abs=1e-6)     # Q → η·Q
    assert make_state("coherent_1").expected(0.5)["mandel_Q"] == pytest.approx(0.0, abs=1e-6)
    assert make_state("thermal_0.5").expected(1.0)["mandel_Q"] == pytest.approx(0.5, abs=1e-6)
    assert np.isnan(mandel_q(0.0, 0.0))


def test_source_short_and_plan_doc():
    from hhd.sim.state_plans import PHASE_DURATIONS
    assert "Quetschquelle" in source_short(source_model("cat_even_2"))
    assert "Testmuster" in source_short(source_model("coherent_1"))
    assert PHASE_DURATIONS["free_running"] >= 5.0


def test_app_rates_mandel_q_and_squeezing():
    js = (ROOT / "hhd" / "server" / "web" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "hhd" / "server" / "web" / "index.html").read_text(encoding="utf-8")
    assert "sub-poissonsch" in js and "Quellmodell" in js and "resverdict" in html
    assert "Heralded Homodyne Mobile" in html


@pytest.fixture(scope="module")
def sq6(tmp_path_factory):
    from hhd.analysis.pipeline import AnalysisParams, analyze_session
    from hhd.server.devices import SimBackend
    from hhd.sim.analysis_validate import validate_analysis
    from hhd.sim.engine import run
    out = tmp_path_factory.mktemp("sq6") / "s"
    cfg = SimBackend(CFG, time_scale=0.5).make_config("T_SQ6", 3, "kurzphase_squeezed_6dB")
    run(cfg, out)
    r = analyze_session(out, AnalysisParams(n_boot=20, jackknife_groups=0, pca=False, n_boot_tomo=8))
    return out, r, validate_analysis(out, r)


def test_b_soll_and_phase_B(sq6):
    out, r, v = sq6
    assert r["state"]["source"]["model"] == "squeezed"
    pb = r["phase"]["B"]
    assert pb["available"] and pb["n_records"] >= 1000
    vb = v["phase"]["B"]["var_fit_dB"]
    assert vb["true"] == pytest.approx(-3.08, abs=0.02)
    assert vb["value"] < -2.0                                # B zeigt Quetschung (phasenaufgelöst)
    assert abs(vb["pull"]) < 4
    assert v["phase"]["var_fit_dB"]["value"] < -2.0          # A ebenso


def test_mandel_q_error_in_result(sq6):
    _, r, v = sq6
    for g in ("A", "B"):
        R = r["reconstruction"][g]
        assert np.isfinite(R["mandel_Q"]) and R["mandel_Q_err"] > 0 and R["g2_err"] > 0
        assert R["mandel_Q"] > 0                             # gequetschtes Vakuum: keine sub-poissonsche Statistik
        assert abs(v["groups"][g]["mandel_Q"]["pull"]) < 4
