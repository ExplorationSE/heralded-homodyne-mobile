"""Laufplan, stochastische Drift, Allan-Abweichung und schnelle Studie (Vorschlag M1)."""
import numpy as np
import pytest

from hhd.sim.check import check_run, interpolation_weights
from hhd.sim.config import ConfigError, SimConfig
from hhd.sim.drift import DriftPath, SCENARIOS, allan_deviation, fit_noise_model, scenario_overrides
from hhd.sim.engine import run, simulate
from hhd.sim.plan import (PlanSpec, adapt_plan, drift_error, make_plan, moments_fock1, n_for_sigma_eta,
                          sample_fock1, sigma_eta, statistics)
from hhd.sim.study import evaluate, monte_carlo, simulate_fast  # noqa: F401


def test_linear_drift_unchanged_without_stochastic_terms():
    """Ohne Irrfahrt/Sinus bleibt die Engine bitgleich zum linearen Modell (kein DriftPath)."""
    cfg = SimConfig.from_dict({"drift": {"lo_rel_per_s": 0.05}, "blocks": [dict(id="v", kind="vacuum", duration_s=0.2)]})
    res = simulate(cfg)
    assert res.drift is None
    assert res.blocks[0].expected["lo_scale"] == pytest.approx(1 + 0.05 * 0.1)


def test_drift_path_statistics():
    cfg = SimConfig.from_dict({"drift": {"lo_rw_per_sqrt_s": 1e-3, "grid_dt_s": 0.5}})
    incr = []
    for k in range(200):
        p = DriftPath(cfg.drift, cfg.lo_monitor, 0.0, 400.0, np.random.default_rng(k))
        incr.append(p.lo(400.0) - p.lo(0.0))
    assert np.std(incr) == pytest.approx(1e-3 * np.sqrt(400), rel=0.15)       # q·√τ
    p = DriftPath(SimConfig.from_dict({"drift": {"lo_sin_amp": 0.01, "lo_sin_period_s": 100.0}}).drift,
                  cfg.lo_monitor, 0.0, 200.0, np.random.default_rng(1))
    t = np.linspace(0, 200, 4001)
    assert np.ptp(p.lo(t)) == pytest.approx(0.02, rel=0.01)


def test_stochastic_drift_in_engine_and_truth(tmp_path):
    cfg = SimConfig.from_dict({**scenario_overrides("schlecht"),
                               "blocks": [dict(id="kv0", kind="vacuum", duration_s=0.3),
                                          dict(id="a", kind="heralded", duration_s=0.3),
                                          dict(id="kv1", kind="vacuum", duration_s=0.3)]})
    res = run(cfg, tmp_path)
    assert res.drift is not None and res.drift.stochastic
    import h5py
    with h5py.File(tmp_path / "truth.h5") as f:
        assert {"t_grid", "lo", "eta_factor", "monitor_gain"} <= set(f["drift"])
        lo = f["drift/lo"][:]
    assert np.all(lo > 0.9) and np.all(lo < 1.1)
    # gleiche Drift-Realisierung in der schnellen Simulation
    from hhd.sim.study import _drift_path
    path, _ = _drift_path(cfg)
    assert np.allclose(path.lo(res.drift.tg), lo)


@pytest.mark.parametrize("bad", [{"drift": {"lo_rw_per_sqrt_s": -1}}, {"drift": {"lo_sin_amp": 0.6}},
                                 {"drift": {"grid_dt_s": 0}}, {"lo_monitor": {"gain_rw_per_sqrt_s": -1e-6}}])
def test_drift_validation(bad):
    with pytest.raises(ConfigError):
        SimConfig.from_dict(bad).validate()


def test_allan_deviation_slopes():
    rng = np.random.default_rng(5)
    white = rng.normal(0, 1e-3, 2 ** 14)
    tau, ad, _ = allan_deviation(white, 1.0)
    assert ad[0] == pytest.approx(1e-3, rel=0.05)
    assert np.polyfit(np.log(tau[:8]), np.log(ad[:8]), 1)[0] == pytest.approx(-0.5, abs=0.07)
    rw = np.cumsum(rng.normal(0, 1e-4, 2 ** 14))                  # q = 1e-4/√s
    tau, ad, n = allan_deviation(rw, 1.0)
    m = (tau >= 4) & (tau <= 512)
    assert np.median(ad[m] / (1e-4 * np.sqrt(tau[m] / 3))) == pytest.approx(1.0, rel=0.15)
    fit = fit_noise_model(*allan_deviation(white + rw, 1.0))
    assert fit["q_rw"] == pytest.approx(1e-4, rel=0.3) and fit["white"] == pytest.approx(1e-3, rel=0.3)


def test_make_plan_structure_and_statistics():
    bl = make_plan(PlanSpec(n_cycles=4, kv_every=2))
    kinds = [b["kind"] for b in bl]
    assert kinds[0] == "vacuum" and kinds[1] == "herald_blocked" and kinds[-1] == "vacuum"
    assert kinds.count("heralded") == 5 and kinds.count("free_running") == 4 and kinds.count("vacuum") == 3
    SimConfig.from_dict({"blocks": bl}).validate()
    st = statistics(bl, R_herald=1000.0)
    assert st["N"]["heralded"] == pytest.approx(5 * 60 * st["R_accepted"])
    assert st["sigma_eta"] == pytest.approx(sigma_eta(0.7 * 1000 / 1020, st["N"]["heralded"], st["N"]["vacuum"]))
    # Momente von |1⟩ nach Verlust
    x = sample_fock1(np.random.default_rng(0), 0.7, 400000)
    m2, m4 = moments_fock1(0.7)
    assert np.mean(x ** 2) == pytest.approx(m2, rel=0.01) and np.mean(x ** 4) == pytest.approx(m4, rel=0.02)
    assert n_for_sigma_eta(0.7, 0.005, 50000) == float("inf")        # 50 000 K-V allein: σ_vac ≈ 0,0076
    assert n_for_sigma_eta(0.7, 0.01, 50000) < 1e5


def test_drift_error_ordering_and_monte_carlo_agreement():
    bl = make_plan(PlanSpec(T_A=20, T_B=10, n_cycles=6, kv_every=2, T_KV=10, T_KH=10))
    dr = dict(SCENARIOS["schlecht"]); dr.update(lo_rw_per_sqrt_s=2e-3, lo_sin_amp=0.0)    # verstärkt
    de = {m: drift_error(bl, dr, m) for m in ("pooled", "interpolated", "monitor")}
    assert de["monitor"]["A"] < de["interpolated"]["A"] < de["pooled"]["A"]
    cfg = SimConfig.from_dict({"drift": {k: v for k, v in dr.items() if k != "gain_rw_per_sqrt_s"},
                               "lo_monitor": {"gain_rw_per_sqrt_s": dr["gain_rw_per_sqrt_s"]}, "blocks": bl})
    mc = monte_carlo(cfg, n_rep=40, seed0=100)
    st = statistics(bl)
    for m in ("pooled", "interpolated"):
        pred = np.sqrt(st["sigma_eta_stat_A"] ** 2 + de[m]["cal_stat_A"] ** 2 + de[m]["A"] ** 2)
        assert mc[m]["rms_A"] == pytest.approx(pred, rel=0.35), m


def test_engine_drift_bias_matches_model(tmp_path):
    """Volle Engine unter starker Drift: der Normierungsfehler je Normierung entspricht dem Modell der schnellen
    Studie (Mittel von P_LO(t)·⟨x²⟩ + ½/S über die Records), berechnet aus der Wahrheit dieser Realisierung."""
    over = {"drift": {"lo_rw_per_sqrt_s": 0.02, "lo_sin_amp": 0.15, "lo_sin_period_s": 9.0, "grid_dt_s": 0.01},
            "session": {"seed": 78}, "source": {"R_herald": 4000.0}}
    bl = make_plan(PlanSpec(T_A=1.0, T_B=0.5, n_cycles=2, T_KV=0.5, kv_every=0, T_KH=0.2))
    cfg = SimConfig.from_dict({**over, "blocks": bl})
    run(cfg, tmp_path)
    from hhd.sim.check import SessionReader
    rr = SessionReader(tmp_path)
    el = 0.5 / cfg.clearance
    xi = cfg.source.R_herald / (cfg.source.R_herald + cfg.source.R_dark)
    P = {b: rr.tru[f"blocks/{b}/lo_monitor/lo_rel_true"][:] for b in rr.blocks()}
    eta = {b: rr.expected(b)["eta_state"] for b in rr.blocks()}
    kv = [b for b in rr.blocks() if b.startswith("kv")]
    A = [b for b in rr.blocks() if b.startswith("a_")]
    t_mid = {b: rr.t_mid(b) for b in rr.blocks()}
    t_kv = np.array([t_mid[b] for b in kv])
    vkv = {b: np.mean(P[b] * 0.5 + el) for b in kv}
    nkv = np.array([len(P[b]) for b in kv], float)
    s2_pool = sum(vkv[b] * len(P[b]) for b in kv) / nkv.sum() / 0.5
    lo_vac = np.average([P[b].mean() for b in kv], weights=nkv)
    rr.close()
    for mode in ("pooled", "interpolated", "monitor"):
        num = den = 0.0
        for b in A:
            if mode == "pooled":
                s2 = s2_pool
            elif mode == "interpolated":
                s2 = float(np.interp(t_mid[b], t_kv, [vkv[k] for k in kv]) / 0.5)
            else:
                s2 = s2_pool * P[b].mean() / lo_vac
            num += np.sum(P[b] * (0.5 + xi * eta[b]) + el) / s2
            den += len(P[b])
        r = check_run(tmp_path, n_boot=5, norm_mode=mode)
        h = r["comparison"]["heralded"]
        pred_bias = (num / den - 0.5) - (h["expected"]["var_x"] - 0.5)
        got_bias = h["var_x"] - h["expected"]["var_x"]
        sig = np.hypot(h["var_err"], h["var_x"] * np.sqrt(2 / nkv.sum() * (2 if mode == "interpolated" else 1)))
        assert abs(pred_bias) > 3 * sig or mode == "monitor"           # Drift ist hier deutlich größer als die Statistik
        assert abs(got_bias - pred_bias) < 4 * sig, (mode, got_bias, pred_bias, sig)


def test_aba_interpolated_includes_calibration():
    assert np.allclose(interpolation_weights(5.0, np.array([0.0, 10.0])), [0.5, 0.5])
    assert np.allclose(interpolation_weights(-1.0, np.array([0.0, 10.0])), [1.0, 0.0])
    cfg = SimConfig.from_dict({"blocks": make_plan(PlanSpec(T_A=10, T_B=5, n_cycles=2, kv_every=1, T_KV=2, T_KH=2))})
    zs = [evaluate(simulate_fast(SimConfig.from_dict({**cfg.to_dict(), "session": {"seed": s}})), "interpolated")["aba_z"]
          for s in range(60)]
    assert np.std(zs) == pytest.approx(1.0, abs=0.3)


def test_adapt_plan_rule():
    from hhd.sim.plan import kv_spacing_max
    good = adapt_plan(dict(q_rw=1e-5, lin=0.0), R_herald=10000.0)
    bad = adapt_plan(dict(q_rw=3e-4, lin=0.0), R_herald=1000.0, n_cycles_min=6)
    assert good["feasible"] and good["L_max_s"] >= bad["L_max_s"]
    assert good["sigma_eta_total"] <= 0.005 and good["sigma_W00"] <= 0.002
    sp = bad["spec"]
    cyc = sp["T_A"] + sp["T_B"]
    if bad["L_max_s"] < bad["statistics"]["t_total_s"]:          # zu starke Drift → K-V-Zwischenläufe
        assert sp["kv_every"] >= 1 and sp["kv_every"] * cyc <= bad["L_max_s"] + 1e-9
    if bad["feasible"]:
        assert bad["drift_error"]["A"] <= 0.005 * np.sqrt(1 / 3) + 1e-12
    assert "monitor" in adapt_plan(dict(q_rw=5e-3), R_herald=1000.0)["hint"]      # zu schnell für K-V
    # Abstand monoton in der Irrfahrtstärke
    L = [kv_spacing_max(dict(q_rw=q), 0.003) for q in (1e-4, 5e-4, 2e-3)]
    assert L[0] >= L[1] >= L[2]


def test_cli_plan(tmp_path, capsys):
    from hhd.sim.cli import main
    out = tmp_path / "plan.yaml"
    assert main(["plan", "--rate", "1000", "-o", str(out)]) == 0
    assert "σ(η)" in capsys.readouterr().out
    cfg = SimConfig.load(out)
    assert not cfg.validate() and [b.kind for b in cfg.blocks][0] == "vacuum"
    t = np.arange(0, 3600, 1.0)
    y = 1 + np.cumsum(np.random.default_rng(1).normal(0, 1e-4, t.size))
    np.savetxt(tmp_path / "s.csv", np.c_[t, y], delimiter=",")
    assert main(["plan", "--allan", str(tmp_path / "s.csv"), "--rate", "1000"]) == 0
    assert "K-V-Abstand" in capsys.readouterr().out
