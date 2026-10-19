"""Weitere geheraldete Zustände und Laufpläne „kurz_<zustand>“.

* Sollwerte (phasengemittelt) gegen Monte Carlo: Varianz, W(0,0) und Quadraturhistogramm.
* Jeder neue Laufplan läuft durch Simulation und Auswertung; die Ergebnisse passen zu den Sollwerten.
* Datenstrom-Simulator und Datenstrom-Auswertung mit einem phasenempfindlichen Zustand.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from hhd.analysis.pipeline import AnalysisParams, analyze_session
from hhd.server.devices import SimBackend, StreamSimBackend
from hhd.sim.analysis_validate import validate_analysis
from hhd.sim.engine import run
from hhd.sim.state_plans import STATE_PLAN_KEYS, STATE_PLANS
from hhd.sim.states import STATE_PRESETS, hermite_function, loss_channel, make_state, state_info

CFG = Path(__file__).resolve().parents[1] / "config" / "standard.yaml"


@pytest.mark.parametrize("key", sorted(STATE_PRESETS))
def test_preset_mc_matches_phase_averaged_expectation(key):
    """MC (zufällige Phase, Verlust η) gegen ρ: Var x = ⟨n⟩ + ½, Histogramm = Σ ρ_nn ψ_n², W(0,0)."""
    st, eta = make_state(key), 0.6
    rng = np.random.default_rng(7)
    N = 200_000
    x = st.measure(rng, rng.uniform(0, 2 * np.pi, N), eta)
    e = st.expected(eta)
    assert abs(x.var() - e["var_x_phase_avg"]) < 5 * e["var_x_phase_avg"] * np.sqrt(2 / N) * 1.5
    rho = loss_channel(st.rho(), eta)
    d = np.real(np.diag(rho))
    assert abs(e["var_x_phase_avg"] - (float(np.arange(len(d)) @ d) + 0.5)) < 1e-6
    # W(0,0) = (1/π) Σ (−1)^n ρ_nn
    assert abs(e["W00"] - float(((-1.0) ** np.arange(len(d))) @ d) / np.pi) < 1e-6
    edges = np.linspace(-5, 5, 41)
    h, _ = np.histogram(x, edges)
    xc = 0.5 * (edges[1:] + edges[:-1]); dx = edges[1] - edges[0]
    dens = sum(d[n] * hermite_function(n, xc) ** 2 for n in range(len(d)) if d[n] > 1e-12)
    mu = N * dens * dx
    m = mu > 20
    chi2 = float(np.sum((h[m] - mu[m]) ** 2 / mu[m]))
    assert chi2 / m.sum() < 2.0, (key, chi2, m.sum())


def test_squeezed_quadrature_variance_vs_phase():
    st = make_state("squeezed_6dB")
    rng = np.random.default_rng(3)
    r = st.r
    for th, v in ((0.0, np.exp(-2 * r) / 2), (np.pi / 2, np.exp(2 * r) / 2)):
        x = st.sample(rng, np.full(100_000, th))
        assert abs(x.var() / v - 1) < 0.02


def test_state_info_and_phase_flags():
    for k in STATE_PLAN_KEYS:
        i = state_info(k)
        assert i["preset"] == k and i["label"]
    assert not state_info("fock2")["phase_sensitive"] and not state_info("thermal_0.5")["phase_sensitive"]
    assert state_info("cat_odd_2")["phase_sensitive"] and state_info("fock01")["phase_sensitive"]
    assert abs(state_info("fock3")["W00_ideal"] + 1 / np.pi) < 1e-9
    assert abs(state_info("squeezed_3dB")["W00_ideal"] - 1 / np.pi) < 1e-6


def test_plans_registered_in_server():
    from hhd.server.service import PLAN_DOC, PLAN_GROUPS, PLANS
    for p, k in STATE_PLANS.items():
        assert PLANS[p].startswith("Kurz: ") and PLANS[p].endswith("(7 Läufe)") and p in PLAN_DOC
    assert PLANS["kurz_fock2"] == "Kurz: Fock |2⟩ (7 Läufe)"
    assert sorted(p for g in PLAN_GROUPS for p in g["plans"]) == sorted(PLANS)
    cfg = SimBackend(CFG).make_config("X", 1, "kurz_cat_even_2")
    assert cfg.source.heralded_state == "cat_even_2" and cfg.phase.mode == "random"
    assert [b.kind for b in cfg.blocks].count("heralded") == 2
    cfg = StreamSimBackend(CFG).make_config("X", 1, "kurz_fock3")
    assert cfg.source.heralded_state == "fock3"
    with pytest.raises(ValueError):
        SimBackend(CFG).make_config("X", 1, "kurz_unbekannt")


def _check(v, key, zmax=4.5):
    A = v["groups"]["A"]
    assert v["state"] == key
    for q in ("var_x", "W00", "eta_n"):
        assert A[q]["pull"] is not None and abs(A[q]["pull"]) < zmax, (key, q, A[q])
    assert "eta_fit" not in A
    assert abs(v["eta_state"]["pull"]) < zmax, (key, v["eta_state"])
    assert abs(v["xi"]["diff"]) < 0.02


@pytest.mark.parametrize("key", STATE_PLAN_KEYS)
def test_state_plan_sim_and_analysis(tmp_path, key):
    """Laufplan kurz_<zustand> (zeitlich verkürzt) → Simulation → Auswertung → Sollwertvergleich."""
    cfg = SimBackend(CFG, time_scale=0.35).make_config("T_" + key.upper().replace(".", ""), 1, f"kurz_{key}")
    out = tmp_path / "s"
    run(cfg, out)
    r = analyze_session(out, AnalysisParams(n_boot=40, jackknife_groups=0, pca=False))
    assert r["state"]["preset"] == key
    assert r["state"]["phase_sensitive"] == state_info(key)["phase_sensitive"]
    assert "eta_eff_n" in r["efficiency_budget"]
    _check(validate_analysis(out, r), key)
    rep = (out / "analysis" / "report.md").read_text(encoding="utf-8")
    assert STATE_PRESETS[key]["label"] in rep


def test_state_plan_stream_engine(tmp_path):
    """Datenstrom: Simulator in Zeitscheiben + Datenstrom-Auswertung mit Katze (phasengemittelt)."""
    from hhd.analysis.streaming import analyze_session_streaming
    from hhd.sim.streaming import StreamingSimulator
    key = "cat_odd_1"
    cfg = StreamSimBackend(CFG, time_scale=0.35).make_config("T_STREAM_CAT", 1, f"kurz_{key}")
    out = tmp_path / "s"
    StreamingSimulator(cfg, slice_s=0.25).run(out)
    r = analyze_session_streaming(out, AnalysisParams(n_boot=40, jackknife_groups=0, pca=False))
    assert r["state"]["preset"] == key and r["state"]["phase_averaged"]
    _check(validate_analysis(out, r), key)


def test_fock1_unchanged(tmp_path):
    """Standard-Kurzplan: Zustand fock1, η-Fit wie bisher, kein eta_n."""
    cfg = SimBackend(CFG, time_scale=0.35).make_config("T_F1", 1, "kurz")
    out = tmp_path / "s"
    run(cfg, out)
    r = analyze_session(out, AnalysisParams(n_boot=20, jackknife_groups=0, pca=False))
    assert r["state"]["preset"] == "fock1"
    assert "eta_n" not in r["reconstruction"]["A"] and "eta_eff_n" not in r["efficiency_budget"]
    v = validate_analysis(out, r)
    assert "eta_fit" in v["groups"]["A"]
