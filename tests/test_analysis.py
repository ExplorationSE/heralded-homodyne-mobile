"""Auswertung: Modenextraktion, PCA, Kalibrierung, Rekonstruktion, Vergleich A/B, Bericht, CLI, Validierung."""
import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from conftest import ROOT, small_standard
from hhd.analysis.pipeline import SCHEMA, AnalysisParams, analyze_session
from hhd.calib.calibrate import CalibParams, Calibration, drift_vacuum_lo
from hhd.modes.extract import ModeSpec, project_records, template_bank
from hhd.recon import core as C
from hhd.recon.phase import phase_fit, reconstruct_with_phase
from hhd.recon.stats import w00_linear, w00_score_coeffs
from hhd.sim.analysis_validate import summarize_pulls, validate_analysis
from hhd.sim.config import BlockCfg
from hhd.sim.engine import run


def lossy1(rng, n, eta):
    x1 = np.sqrt(rng.gamma(1.5, 1.0, n)) * rng.choice([-1.0, 1.0], n)
    return np.sqrt(eta) * x1 + np.sqrt(1 - eta) * rng.normal(0, np.sqrt(0.5), n)


# ------------------------------------------------------------------ Rekonstruktion (Quadraturebene)
def test_patterns_and_loss_matrix():
    x = np.linspace(-8, 8, 4001)
    P = C.patterns(x, 6)
    assert np.allclose(np.trapezoid(P, x, axis=1), 1, atol=1e-6)
    B = C.loss_matrix(6, 0.7)
    assert np.allclose(B.sum(0), 1)
    Pl = C.patterns(x, 6, eta=0.6)
    assert np.allclose(np.trapezoid(Pl, x, axis=1), 1, atol=1e-6)


def test_wigner_fidelity_stats():
    assert C.wigner00(np.array([1.0])) == pytest.approx(1 / np.pi)
    assert C.wigner00(np.array([0.0, 1.0])) == pytest.approx(-1 / np.pi)
    p = C.lossy_fock1(0.6, 10)
    assert C.wigner00(p) == pytest.approx((1 - 1.2) / np.pi)
    assert C.fidelity_diag(p, p) == pytest.approx(1)
    r = np.linspace(0, 3, 7)
    assert C.wigner_radial(p, r)[0] == pytest.approx(C.wigner00(p))
    s = C.photon_stats(p)
    assert s["mean_n"] == pytest.approx(0.6) and s["g2"] == pytest.approx(0, abs=1e-12)


@pytest.mark.parametrize("est", C.ESTIMATORS)
def test_reconstruct_estimators(est):
    rng = np.random.default_rng(3)
    eta = 0.62
    x = lossy1(rng, 20000, eta)
    r = C.reconstruct(x, 10 if est != "moments" else 2, est)
    assert r.W00 == pytest.approx((1 - 2 * eta) / np.pi, abs=0.02)
    assert r.p.sum() == pytest.approx(1)
    assert np.all(r.p >= -1e-12) or est == "moments"
    if est in ("maxlik", "em"):
        assert r.converged


def test_eta_correction_and_fit():
    rng = np.random.default_rng(4)
    x = lossy1(rng, 20000, 0.6)
    r = C.reconstruct(x, 6, "maxlik", eta=0.6)
    assert r.p[1] == pytest.approx(1, abs=0.05)
    f = C.fit_eta_fock1(x)
    assert f["eta"] == pytest.approx(0.6, abs=4 * f["sigma_fisher"])


def test_w00_linear_matches_moments():
    rng = np.random.default_rng(5)
    x = lossy1(rng, 5000, 0.5)
    w, e = w00_linear(x)
    assert w == pytest.approx(C.reconstruct(x, 2, "moments").W00, abs=1e-9)
    assert 0 < e < 0.05
    a = w00_score_coeffs(2)
    assert a @ np.array([1, 0.5, 0.75]) == pytest.approx(1 / np.pi)       # Vakuum-Momente


def test_phase_interface():
    rng = np.random.default_rng(6)
    th = rng.uniform(0, 2 * np.pi, 20000)
    x = 0.8 * np.cos(th - 0.4) + rng.normal(0, np.sqrt(0.5), th.size)
    f = phase_fit(x, th)
    assert f["A"] == pytest.approx(0.8, abs=5 * f["A_err"]) and f["phi"] == pytest.approx(0.4, abs=0.05)
    with pytest.raises(NotImplementedError):
        reconstruct_with_phase(x, th)


# ------------------------------------------------------------------ Moden
def test_template_bank_projection():
    spec = ModeSpec("double_exponential", 1 / 40e-9, 2.5e9, 1e9)
    bank = template_bank(spec)
    assert np.allclose((bank.T ** 2).sum(1), 1, atol=1e-9)
    assert bank.bandwidth_effect < 1e-4
    L = 5000
    pos = np.array([2500.0, 2500.37, 1200.8])
    data = np.zeros((3, L))
    k = np.arange(L)
    for i, p in enumerate(pos):
        data[i] = 7.0 * C_mode(spec, k - p) + 3.0
    q, cov = project_records(data, pos, bank, offset=3.0)
    assert np.allclose(q, 7.0, rtol=2e-3) and np.all(cov > 0.999)


def C_mode(spec, u):
    from hhd.modes.extract import mode_function
    f = mode_function(spec, u)
    return f / np.sqrt(np.sum(f ** 2))


def test_gauss_mode_spec():
    s = ModeSpec("gauss", 1 / 40e-9, 2.5e9)
    assert s.half > 0 and template_bank(s).T.shape[1] == 2 * s.half + 1


# ------------------------------------------------------------------ Drift (synthetisch)
def test_drift_flags():
    kv = [dict(block="kv0", t=0.0, var=100.0, n=20000, lo_rel=1.0), dict(block="kv1", t=10.0, var=106.0, n=20000, lo_rel=1.02)]
    cal = Calibration("pooled", 0.0, 102.0, 40000, kv, None, "–", None, "–", None, lo_rel={"a": 1.02, "b": 1.0})
    d = drift_vacuum_lo(cal, CalibParams())
    assert d["vacuum"]["z"] > 3 and any("K-V" in f for f in d["flags"]) and any("LO-Monitor" in f for f in d["flags"])


# ------------------------------------------------------------------ Kette auf simulierten Sitzungen
@pytest.fixture(scope="module")
def ana_std(std_run, tmp_path_factory):
    d = tmp_path_factory.mktemp("ana") / "s"
    shutil.copytree(std_run[1], d)
    shutil.rmtree(d / "sync", ignore_errors=True); shutil.rmtree(d / "analysis", ignore_errors=True)
    r = analyze_session(d, AnalysisParams(n_boot=40))
    return d, r


def el_config(seed=21):
    cfg = small_standard(seed)
    cfg.blocks = [BlockCfg(id="kv0", kind="vacuum", duration_s=0.5, periodic_rate=4000),
                  BlockCfg(id="dark", kind="electronic", duration_s=0.3, periodic_rate=4000),
                  BlockCfg(id="a1", kind="heralded", duration_s=1.5),
                  BlockCfg(id="b1", kind="free_running", duration_s=0.4),
                  BlockCfg(id="a2", kind="heralded", duration_s=1.5),
                  BlockCfg(id="kv1", kind="vacuum", duration_s=0.5, periodic_rate=4000)]
    return cfg


@pytest.fixture(scope="module")
def ana_el(tmp_path_factory):
    d = tmp_path_factory.mktemp("el")
    run(el_config(), d)
    return d, analyze_session(d, AnalysisParams(n_boot=30))


def test_result_schema_and_files(ana_std):
    d, r = ana_std
    out = Path(r["_out_dir"])
    js = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert js["schema"] == SCHEMA
    for k in ("calibration", "mode", "crosstalk", "pca", "reconstruction", "comparison", "drift_AB",
              "efficiency_budget", "warnings", "performance", "groups"):
        assert k in js, k
    assert (out / "quadratures.h5").exists() and (out / "report.md").read_text(encoding="utf-8").startswith("# Auswertung")
    import h5py
    with h5py.File(out / "quadratures.h5") as f:
        assert "x" in f["runs/heralded"] and "groups/A/heralded" in f


def test_std_physics(ana_std):
    d, r = ana_std
    A, B = r["reconstruction"]["A"], r["reconstruction"]["B"]
    assert {"B_interleaved"} <= set(r["reconstruction"])          # eingestreute Zufallstrigger getrennt
    assert A["var_x"] > 0.9 and abs(B["var_x"] - 0.5) < 5 * B["var_x_err"]
    assert A["W00"] < 0.05 and A["W00_err"] > 0
    c = r["comparison"]
    assert c["ks"]["pvalue"] < 1e-6 and c["diff_var_x"]["z"] > 10
    assert len(c["histogram"]["A"]) == len(c["histogram"]["edges"]) - 1
    assert not r["crosstalk"]["flags"]
    assert r["pca"]["overlap_param_corrected"] > 0.95
    assert 0.85 < r["efficiency_budget"]["xi"] < 0.95


def test_validation_pulls(ana_std):
    d, r = ana_std
    v = validate_analysis(d, r)
    for q in ("var_x", "W00", "eta_fit"):
        assert abs(v["groups"]["A"][q]["pull"]) < 4, q
    assert abs(v["eta_comparison"]["pull"]) < 4
    s = summarize_pulls([v])
    assert s["A.var_x"]["n"] == 1


def test_bootstrap_includes_vacuum(ana_std):
    d, r = ana_std
    r0 = analyze_session(d, AnalysisParams(n_boot=40, boot_vacuum=False, pca=False), write=False)
    assert r["reconstruction"]["A"]["var_x_err"] > r0["reconstruction"]["A"]["var_x_err"]


@pytest.mark.parametrize("norm", ["interpolated", "monitor", "monitor_record"])
def test_norm_variants(ana_std, norm):
    d, r = ana_std
    rn = analyze_session(d, AnalysisParams(n_boot=0, norm=norm, pca=False), write=False)
    assert rn["reconstruction"]["A"]["var_x"] == pytest.approx(r["reconstruction"]["A"]["var_x"], rel=0.03)


def test_mode_source_pca(ana_std):
    d, r = ana_std
    rp = analyze_session(d, AnalysisParams(n_boot=0, mode_source="pca"), write=False)
    assert rp["mode"]["shape"] == "custom"
    assert rp["reconstruction"]["A"]["var_x"] == pytest.approx(r["reconstruction"]["A"]["var_x"], abs=0.02)


def test_electronic_calibration(ana_el):
    d, r = ana_el
    c = r["calibration"]
    assert c["el_source"].startswith("Dunkellauf")
    assert c["eta_el"] == pytest.approx(0.9693, abs=0.01)
    assert c["dark_b_source"] == "Dunkellauf (LO aus)"
    v = validate_analysis(d, r)
    assert abs(v["eta_el"]["diff"]) < 0.01
    assert "A_halves" in r["drift_AB"] and len(r["drift_AB"]["ABA"]) == 1


def test_clearance_parameter(ana_std):
    d, _ = ana_std
    r = analyze_session(d, AnalysisParams(n_boot=0, pca=False, clearance_dB=15.0), write=False)
    assert r["calibration"]["eta_el"] == pytest.approx(10 ** 1.5 / (1 + 10 ** 1.5))


def test_analysis_does_not_read_truth():
    for sub in ("analysis", "calib", "modes", "recon"):
        for f in (ROOT / "hhd" / sub).glob("*.py"):
            assert "truth" not in f.read_text(encoding="utf-8").replace("Wahrheit", ""), f


def test_cli_analysis(ana_std, capsys, tmp_path):
    from hhd.cli import main
    d, _ = ana_std
    assert main(["compare", str(d), "--n-boot", "5", "--no-pca", "-o", str(tmp_path / "o")]) == 0
    assert (tmp_path / "o" / "result.json").exists()
    assert main(["report", str(tmp_path / "o" / "result.json")]) == 0
    assert "Vergleich A/B" in capsys.readouterr().out
    assert main(["validate-analysis", str(d)]) == 0
    assert "eta_comparison" in capsys.readouterr().out
    assert main(["calibrate", str(d), "--no-pca", "-o", str(tmp_path / "c")]) == 0
    assert main(["reconstruct", str(d), "--n-boot", "3", "--no-pca", "--estimator", "em", "-o", str(tmp_path / "r")]) == 0


def test_mode_source_pca_raw_biased(ana_std):
    d, r = ana_std
    rp = analyze_session(d, AnalysisParams(n_boot=0, mode_source="pca_raw"), write=False)
    assert any("PCA" in w for w in rp["warnings"])


def test_pca_unavailable_and_binned_switch(ana_std):
    from hhd.modes.pca import pca_mode
    assert pca_mode([], [], 0.0, 1.0, ModeSpec("double_exponential"))["available"] is False
    d, r = ana_std
    rb = analyze_session(d, AnalysisParams(n_boot=5, pca=False, binned_above=100), write=False)
    assert rb["reconstruction"]["A"]["estimator"] == "binned"
    assert rb["reconstruction"]["A"]["W00"] == pytest.approx(r["reconstruction"]["A"]["W00"], abs=2e-3)


def test_crosstalk_detected(tmp_path):
    cfg = small_standard(31)
    cfg.blocks = [BlockCfg(id="kv0", kind="vacuum", duration_s=0.3, periodic_rate=4000),
                  BlockCfg(id="kh", kind="herald_blocked", duration_s=0.6, crosstalk_amp=0.3),
                  BlockCfg(id="a1", kind="heralded", duration_s=0.5),
                  BlockCfg(id="kv1", kind="vacuum", duration_s=0.3, periodic_rate=4000)]
    run(cfg, tmp_path)
    r = analyze_session(tmp_path, AnalysisParams(pca=False), stages=(), write=False)
    xt = r["crosstalk"]
    assert xt["waveform"]["max_abs_z"] > 5 and any("Übersprechen" in f for f in xt["flags"])


def test_crosstalk_null(ana_std):
    assert ana_std[1]["crosstalk"]["waveform"]["max_abs_z"] < 5
