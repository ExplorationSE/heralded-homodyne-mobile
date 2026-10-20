"""Phasenabhängigkeit (ab 0.15): Piezo-Rampe im LO-Arm, K-θ-Kalibrierung, MaxLik-Tomografie mit bekannter Phase.

* Simulator: Rampenspur je Record (Schema 1.3), wahre Phase in der Wahrheit.
* θ-Kalibrierung gegen theta_true; MaxLik-Treue für kohärent, gequetscht, Katze; Quetschachse.
* Laufplan kurzphase_<zustand> → Simulation → Auswertung (Speicher und Datenstrom) → Sollwertvergleich.
* Rückwärtskompatibilität: alte Dateien (Schema 1.2, ohne Rampe) → phasengemittelt; |1⟩ unverändert.
"""
from __future__ import annotations

from pathlib import Path

import h5py
import numpy as np
import pytest

from hhd.analysis.pipeline import AnalysisParams, analyze_session
from hhd.io.session import load_session
from hhd.recon import tomo as T
from hhd.server.devices import SimBackend, StreamSimBackend
from hhd.sim.analysis_validate import validate_analysis
from hhd.sim.engine import run
from hhd.sim.state_plans import PHASE_PLANS
from hhd.sim.states import loss_channel, make_state

CFG = Path(__file__).resolve().parents[1] / "config" / "standard.yaml"
AP = dict(n_boot=20, jackknife_groups=0, pca=False, n_boot_tomo=8)


@pytest.fixture(scope="module")
def cat_phase(tmp_path_factory):
    out = tmp_path_factory.mktemp("catphase") / "s"
    cfg = SimBackend(CFG, time_scale=0.5).make_config("T_PH_CAT", 3, "kurzphase_cat_even_2")
    run(cfg, out)
    r = analyze_session(out, AnalysisParams(**AP))
    return out, r, validate_analysis(out, r)


def test_phase_plans_registered():
    from hhd.server.service import PLAN_GROUPS, PLANS
    assert set(PHASE_PLANS) == {f"kurzphase_{k}" for k in ("fock01", "cat_even_1", "cat_odd_1", "cat_even_2", "cat_odd_2",
                                                           "kitten_3dB", "coherent_1", "squeezed_3dB", "squeezed_6dB")}
    assert PLANS["kurzphase_cat_even_2"] == "Kurz mit Phase: Katze gerade α=2 (10 Läufe)"
    g = [g for g in PLAN_GROUPS if g["label"] == "Mit Phase (Piezo-Rampe)"][0]
    assert g["plans"] == list(PHASE_PLANS)
    cfg = SimBackend(CFG).make_config("X", 1, "kurzphase_squeezed_6dB")
    assert cfg.phase.mode == "piezo" and cfg.phase.ramp_channel == "aux_adc"
    assert [b.kind for b in cfg.blocks] == ["vacuum", "electronic", "phase_ref", "herald_blocked", "heralded", "phase_ref",
                                            "free_running", "heralded", "phase_ref", "vacuum"]
    assert StreamSimBackend(CFG).make_config("X", 1, "kurzphase_fock01").source.heralded_state == "fock01"


def test_ramp_recorded_schema_13(cat_phase):
    out, r, v = cat_phase
    with h5py.File(out / "digitizer.h5", "r") as f:
        assert f.attrs["schema_version"] in ("1.3", b"1.3")
    s = load_session(out)
    kinds = {x.block_id: x.dataset_type for x in s.runs}
    assert [k for k, t in kinds.items() if t == "phase_reference"] == ["kt_00", "kt_01", "kt_02"]
    for x in s.runs:
        assert x.has_ramp
        V = x.read_ramp()
        assert len(V) == x.n_records_file and -0.05 <= np.nanmin(V) and np.nanmax(V) <= 10.5
    with h5py.File(out / "truth.h5", "r") as f:
        th = f["blocks/kt_00/records/theta_true"][:]
        assert np.isfinite(th).mean() > 0.99


def test_theta_calibration_vs_truth(cat_phase):
    out, r, v = cat_phase
    P = r["phase"]
    assert P["available"] and P["source"] == "ramp" and P["label"] == "Phase bekannt (Piezo-Rampe, kalibriert)"
    q = P["calibration"]["quality"]
    assert 6.5 < q["stroke_rad"] < 9.5 and q["chi2_ndof"] < 1.3
    t = v["phase"]["theta"]
    assert t["rms"] < 0.08 and abs(t["bias"]) < 0.03
    assert 0.5 < t["sigma_reported"] / t["rms"] < 2.0
    with h5py.File(out / "analysis" / "phase.h5", "r") as f:
        g = f["runs/a_01"]
        assert set(g) == {"theta", "theta_err", "theta_source"} and np.all(g["theta_source"][:] == 1)


def test_cat_phase_plan_fidelity(cat_phase):
    out, r, v = cat_phase
    vp = v["phase"]
    assert vp["fidelity_expected"] > 0.97
    for q in ("W00", "mean_n", "alpha_from_a2"):
        assert abs(vp[q]["pull"]) < 4.5, (q, vp[q])
    assert r["phase"]["fidelity"]["F_model"] > 0.97
    assert r["phase"]["coverage"]["ok"]
    W = np.array(r["phase"]["wigner2d"]["W"])
    assert W.shape == (61, 61) and W.min() < -0.005           # Interferenzstreifen der geraden Katze (nach Verlust schwach)
    rep = (out / "analysis" / "report.md").read_text(encoding="utf-8")
    assert "Phase bekannt (Piezo-Rampe, kalibriert)" in rep


def _tomo(key, eta=0.8, n=40000, seed=1, dtheta=0.0):
    st = make_state(key)
    rng = np.random.default_rng(seed)
    th = rng.uniform(0, 2 * np.pi, n)
    x = st.measure(rng, th, eta)
    H = T.PhaseHist(); H.add(x, th + dtheta)
    c, te, xe = H.coarse(3, 3, 6.0)
    N = T.auto_cutoff(T.state_metrics(st.rho())["mean_n"])
    rho = T.Tomography(c, te, xe, eta, N).fit()["rho"]
    return st, rho, H


@pytest.mark.parametrize("key,fmin", [("coherent_1", 0.99), ("squeezed_3dB", 0.99), ("cat_odd_1", 0.98), ("cat_even_2", 0.95)])
def test_maxlik_fidelity(key, fmin):
    st, rho, _ = _tomo(key)
    F, phi = T.fidelity_max_rotation(rho, st.rho(), pure=True)
    assert F > fmin, (key, F)
    sym = np.pi if key != "coherent_1" else 2 * np.pi        # Katzen und gequetschte Zustände: Symmetrie unter π
    d = np.mod(phi + sym / 2, sym) - sym / 2
    assert abs(d) < 0.1, (key, phi)


def test_squeezing_axis_and_dB():
    st, rho, H = _tomo("squeezed_6dB", n=60000)
    m0 = T.state_metrics(st.rho())
    m = T.state_metrics(rho)
    assert abs(m["squeezing_dB"] - m0["squeezing_dB"]) < 0.6
    da = np.angle(np.exp(2j * (m["axis_rad"] - m0["axis_rad"]))) / 2
    assert abs(da) < 0.05
    # Phasenversatz der Zuordnung dreht die Achse um denselben Winkel
    _, rho2, H2 = _tomo("squeezed_6dB", n=60000, dtheta=0.4)
    da2 = np.angle(np.exp(2j * (T.state_metrics(rho2)["axis_rad"] - m0["axis_rad"]))) / 2
    assert abs(da2 - 0.4) < 0.05
    f = T.fit_var_theta(H.var_theta(4))
    eta = 0.8
    vmin = eta * 0.5 * 10 ** (-0.6) + (1 - eta) * 0.5
    assert f["available"] and abs(f["var_min"] - vmin) < 5 * f["var_min_err"] + 0.01


def test_stream_phase_plan(tmp_path):
    """Datenstrom: Simulator in Zeitscheiben + Datenstrom-Auswertung (quadratures.h5) mit gequetschtem Zustand."""
    from hhd.analysis.streaming import analyze_session_streaming
    from hhd.sim.streaming import StreamingSimulator
    cfg = StreamSimBackend(CFG, time_scale=0.4).make_config("T_PH_STREAM", 2, "kurzphase_squeezed_6dB")
    out = tmp_path / "s"
    StreamingSimulator(cfg, slice_s=0.25, record_truth_dir=out).run(out)
    r = analyze_session_streaming(out, AnalysisParams(**AP))
    assert r["phase"]["available"]
    v = validate_analysis(out, r)["phase"]
    assert v["theta"]["rms"] < 0.08
    assert v["fidelity_expected"] > 0.97
    assert abs(v["squeezing_dB"]["diff"]) < 0.5 and abs(v["axis_rad"]["diff"]) < 0.1
    assert (out / "analysis" / "phase.h5").exists()


def test_old_file_without_ramp_falls_back(tmp_path):
    """Schema 1.2 (ohne records/ramp_raw): Datei lesbar, Auswertung phasengemittelt mit Begründung."""
    cfg = SimBackend(CFG, time_scale=0.3).make_config("T_OLD", 1, "kurz_cat_odd_1")
    out = tmp_path / "s"
    run(cfg, out)
    with h5py.File(out / "digitizer.h5", "a") as f:
        f.attrs["schema_version"] = "1.2"
        for b in f["blocks"].values():
            if "ramp_raw" in b["records"]:
                del b["records/ramp_raw"]
    from hhd.io.schema import validate_digitizer_file
    assert not validate_digitizer_file(out / "digitizer.h5")
    assert not any(x.has_ramp for x in load_session(out).runs)
    r = analyze_session(out, AnalysisParams(**AP))
    assert r["phase"]["available"] is False and "phasengemittelt" in r["phase"]["reason"]
    assert "phase" not in validate_analysis(out, r)
    rep = (out / "analysis" / "report.md").read_text(encoding="utf-8")
    assert "phasengemittelt" in rep


def test_fock1_unchanged_by_phase_stage(tmp_path):
    cfg = SimBackend(CFG, time_scale=0.3).make_config("T_F1P", 1, "kurz")
    out = tmp_path / "s"
    run(cfg, out)
    r1 = analyze_session(out, AnalysisParams(**AP), write=False)
    r0 = analyze_session(out, AnalysisParams(**AP, phase=False), write=False)
    assert r1["phase"]["available"] is False and "phase" not in r0
    for k in ("var_x", "W00", "eta_fit"):
        assert r1["reconstruction"]["A"][k] == r0["reconstruction"]["A"][k]


def test_loss_adjoint_consistency():
    """POVM: Tr(L(ρ) O) = Tr(ρ L†(O))."""
    rng = np.random.default_rng(3)
    N = 8
    a = rng.normal(size=(N, N)) + 1j * rng.normal(size=(N, N)); rho = a @ a.conj().T; rho /= np.trace(rho)
    b = rng.normal(size=(N, N)) + 1j * rng.normal(size=(N, N)); O = b + b.conj().T
    lhs = np.trace(loss_channel(rho, 0.7) @ O)
    rhs = np.trace(rho @ T.loss_adjoint(O, 0.7))
    assert abs(lhs - rhs) < 1e-10
