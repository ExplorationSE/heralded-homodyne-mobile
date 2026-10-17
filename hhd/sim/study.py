"""Schnelle Sitzungssimulation auf Quadraturebene für Laufplan-Studien (Vorschlag M1, Laufplan).

Die volle Simulation (:mod:`hhd.sim.engine`) erzeugt Wellenformen und braucht ≈ 1 ms je Record; für
Monte-Carlo-Studien über viele Laufpläne mit 10⁵–10⁶ Records je Sitzung ist das zu langsam. Diese Variante
nutzt **dieselbe Konfiguration, dieselbe Drift-Realisierung** (:class:`hhd.sim.drift.DriftPath`, gleicher
Seed-Zweig wie die Engine), dieselben Lauf-Effizienzen (:func:`hhd.sim.engine.eta_of_run`) und dieselben
Normierungen wie :mod:`hhd.sim.check`, erzeugt aber direkt die projizierten Quadraturen:

* A (heralded): phasengemitteltes |1⟩ nach Verlust η_Lauf (Anteil ξ = R_h/(R_h+R_dark)), sonst Vakuum;
* B (free_running): thermisch mit n_eff wie in den Sollwerten der Engine (≈ Vakuum);
* K-V / K-H: Vakuum; Messwert x = √P_LO(t)·x + elektronisches Rauschen (Varianz ½/S);
* Kanal B: Mittelwert je Lauf aus P_LO·(1 + RIN)·g_B(t) + Rauschen.

Nicht enthalten: ungeheraldete Fremdmoden im Record, Übersprechen, Synchronisation (für den Laufplan ohne
Belang; der Test ``tests/test_plan_study.py`` vergleicht mit der vollen Engine).
"""
from __future__ import annotations

import numpy as np

from .config import SimConfig
from .drift import DriftPath
from .engine import eta_of_run
from .plan import VAC, accepted_rate, sample_fock1

NORMS = ("pooled", "interpolated", "monitor")


def _drift_path(cfg: SimConfig) -> tuple[DriftPath, np.random.Generator]:
    se = cfg.session
    root = np.random.SeedSequence(se.seed)
    root.spawn(2)                                       # wie engine.simulate: Uhr, Blöcke
    t_end = se.start_s + sum(b.duration_s + se.gap_s for b in cfg.blocks) + 1.0
    path = DriftPath(cfg.drift, cfg.lo_monitor, se.start_s, t_end, np.random.default_rng(root.spawn(1)[0]))
    return path, np.random.default_rng(root.spawn(1)[0])


def simulate_fast(cfg: SimConfig, keep_x: bool = False) -> dict:
    """Simuliert alle Läufe einer Sitzung auf Quadraturebene; Rückgabe je Lauf: Zeiten, Wahrheit, Rohvarianz."""
    cfg.validate()
    path, rng = _drift_path(cfg)
    src, d, se, lm = cfg.source, cfg.digitizer, cfg.session, cfg.lo_monitor
    S = cfg.clearance
    sig_el = np.sqrt(VAC / S)
    xi = src.R_herald / (src.R_herald + src.R_dark)
    R_acc = accepted_rate(src.R_herald + src.R_dark, d.record_length, d.pretrigger, cfg.fs, d.rearm)
    runs = []
    t0 = se.start_s
    for b in cfg.blocks:
        T = b.duration_s
        k = b.kind
        if k in ("heralded", "herald_blocked"):
            n = int(rng.poisson(R_acc * T))
        else:
            n = int(np.floor(T * b.periodic_rate))
        t = np.sort(rng.uniform(t0, t0 + T, n))
        P = b.lo_scale * path.lo(t) if k != "electronic" else np.zeros(n)
        t_mid = t0 + T / 2
        eta_run = eta_of_run(cfg, b, t_mid, path)
        lo_mid = float(b.lo_scale * path.lo(t_mid))
        eta_el = 1.0 / (1.0 + 1.0 / (S * lo_mid))
        n_true = 0.0
        if k == "heralded":
            true = rng.random(n) < xi
            x = rng.normal(0, np.sqrt(VAC), n)
            x[true] = sample_fock1(rng, eta_run, int(true.sum()))
            n_true = xi * eta_run * eta_el
        elif k in ("free_running", "random"):
            g = src.gamma
            n_eff = (src.R_herald / src.eta_herald) * eta_run * eta_el * (2.5 / g if src.mode_shape == "double_exponential"
                                                                           else np.sqrt(np.pi) / g)
            x = rng.normal(0, np.sqrt(VAC + n_eff / max(eta_el, 1e-9)), n)   # vor el. Rauschen: n_eff/η_el
            n_true = n_eff
        else:
            x = rng.normal(0, np.sqrt(VAC), n)
        y = np.sqrt(np.clip(P, 0, None)) * x + rng.normal(0, sig_el, n)
        mon = np.nan
        if cfg.lo_b_enabled and n and k != "electronic":
            pm = P * (1 + rng.normal(0, lm.rin_rel, n)) * path.monitor_gain(t)
            pm = pm + rng.normal(0, lm.noise_lsb / lm.lsb_nominal / np.sqrt(cfg.lo_b_record_length), n)
            mon = float(pm.mean())
        r = dict(id=b.id, kind=k, t_mid=t_mid - se.start_s, n=n, m2=float(np.mean(y ** 2)) if n else np.nan,
                 m4=float(np.mean(y ** 4)) if n else np.nan, lo_mid=lo_mid, eta_run=eta_run, n_true=n_true, lo_monitor=mon)
        if keep_x:
            r["x"] = y
        runs.append(r)
        t0 += T + se.gap_s
    return dict(runs=runs, drift=path)


def evaluate(sim: dict, mode: str = "interpolated") -> dict:
    """Normierung wie :mod:`hhd.sim.check` und Kenngrößen der Sitzung (n̂ = Var/Skala − ½ ≙ η̂ für A)."""
    runs = sim["runs"]
    kv = [r for r in runs if r["kind"] == "vacuum"]
    if not kv:
        raise ValueError("kein K-V-Lauf")
    nk = np.array([r["n"] for r in kv], float)
    vk = np.array([r["m2"] for r in kv])
    tk = np.array([r["t_mid"] for r in kv])
    s2_pool = float(np.sum(nk * vk) / nk.sum() / VAC)
    lo_vac = float(np.average([r["lo_monitor"] for r in kv], weights=nk))

    def scale(r):
        if mode == "pooled":
            return s2_pool
        if mode == "interpolated":
            return float(np.interp(r["t_mid"], tk, vk) / VAC)
        if mode == "monitor":
            return s2_pool * r["lo_monitor"] / lo_vac
        raise ValueError(mode)

    out = dict(mode=mode, runs={})
    for role, kind in (("A", "heralded"), ("B", "free_running")):
        rr = [r for r in runs if r["kind"] == kind]
        if not rr:
            continue
        N = np.array([r["n"] for r in rr], float)
        v = np.array([r["m2"] / scale(r) for r in rr])
        m4 = np.array([r["m4"] / scale(r) ** 2 for r in rr])
        est = float(np.sum(N * v) / N.sum() - VAC)
        truth = float(np.sum(N * np.array([r["n_true"] for r in rr])) / N.sum())
        var_m2 = float(np.sum(N * (m4 - v ** 2)) / N.sum() / N.sum())
        out[role] = dict(est=est, truth=truth, err=est - truth, sigma_stat=float(np.sqrt(var_m2)), n=int(N.sum()))
        for r, vi in zip(rr, v):
            out["runs"][r["id"]] = float(vi - VAC)
        if role == "A" and len(rr) >= 2:
            a, b = rr[0], rr[-1]
            sa = np.sqrt((a["m4"] - a["m2"] ** 2) / a["n"]) / scale(a)
            sb = np.sqrt((b["m4"] - b["m2"] ** 2) / b["n"]) / scale(b)
            va, vb = a["m2"] / scale(a), b["m2"] / scale(b)
            cal = 0.0
            if mode == "interpolated":            # K-V-Statistik der verschiedenen Skalen (wie check.aba_calibration_variance)
                from .check import interpolation_weights
                wa, wb = interpolation_weights(a["t_mid"], tk), interpolation_weights(b["t_mid"], tk)
                cal = float(np.sum((vb * wb - va * wa) ** 2 * 2 / nk))
            out["aba_z"] = float((vb - va) / np.sqrt(sa ** 2 + sb ** 2 + cal))
    if "A" in out and "B" in out:
        out["delta"] = dict(est=out["A"]["est"] - out["B"]["est"], truth=out["A"]["truth"] - out["B"]["truth"])
        out["delta"]["err"] = out["delta"]["est"] - out["delta"]["truth"]
    if len(kv) >= 2:
        out["kv_z"] = float((vk[-1] / vk[0] - 1) / np.sqrt(2 / nk[0] + 2 / nk[-1]))
    return out


def monte_carlo(cfg: SimConfig, n_rep: int = 50, modes=NORMS, seed0: int | None = None) -> dict:
    """Wiederholt die Sitzung mit Seeds seed0, seed0+1, …; Mittel, Streuung und rms-Fehler je Normierung."""
    import copy
    seed0 = cfg.session.seed if seed0 is None else seed0
    res = {m: dict(A=[], B=[], delta=[], aba=[], kv=[], sA=[]) for m in modes}
    for k in range(n_rep):
        c = copy.deepcopy(cfg)
        c.session.seed = seed0 + k
        sim = simulate_fast(c)
        for m in modes:
            e = evaluate(sim, m)
            res[m]["A"].append(e["A"]["err"]); res[m]["B"].append(e["B"]["err"]); res[m]["delta"].append(e["delta"]["err"])
            res[m]["aba"].append(e.get("aba_z", np.nan)); res[m]["kv"].append(e.get("kv_z", np.nan))
            res[m]["sA"].append(e["A"]["sigma_stat"])
    out = {}
    for m, r in res.items():
        a = {k: np.asarray(v) for k, v in r.items()}
        out[m] = dict(bias_A=float(a["A"].mean()), rms_A=float(np.sqrt(np.mean(a["A"] ** 2))), sd_A=float(a["A"].std()),
                      bias_delta=float(a["delta"].mean()), rms_delta=float(np.sqrt(np.mean(a["delta"] ** 2))),
                      rms_B=float(np.sqrt(np.mean(a["B"] ** 2))), sigma_stat_A=float(a["sA"].mean()),
                      aba_flag=float(np.mean(np.abs(a["aba"]) > 3)), kv_flag=float(np.mean(np.abs(a["kv"]) > 3)),
                      n_rep=n_rep)
    return out
