"""Laufplan-Studie M1 (Vorschlag): Statistik, Drift, Allan-Analyse, ABA-Nachweis, Zeitraffer mit der vollen Engine.

    python tools/laufplan_studie.py --out /pfad/fig [--quick]

Alle Driftszenarien sind Annahmen (hhd.sim.drift.SCENARIOS); die schnelle Studie (hhd.sim.study) ist gegen die
volle Engine getestet (tests/test_plan_study.py). Ergebnisse: PNG-Abbildungen und ``studie.json``.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from hhd.sim.config import SimConfig  # noqa: E402
from hhd.sim.drift import SCENARIOS, DriftPath, allan_deviation, fit_noise_model, scenario_overrides  # noqa: E402
from hhd.sim.plan import (PlanSpec, accepted_rate, adapt_plan, drift_error, kv_spacing_max, make_plan,  # noqa: E402
                          moments_fock1, sigma_eta, statistics, timeline, w00_coefficient)
from hhd.sim.study import evaluate, monte_carlo, simulate_fast  # noqa: E402

DEFAULT = PlanSpec(T_A=60, T_B=30, n_cycles=6, kv_every=3, T_KV=20, rate_KV=10000, rate_B=2000, T_KH=60)
NORMS = ("pooled", "interpolated", "monitor")
LBL = {"pooled": "gemeinsam (pooled)", "interpolated": "interpoliert", "monitor": "LO-Monitor"}
COL = {"pooled": "C3", "interpolated": "C0", "monitor": "C2"}
plt.rcParams.update({"figure.dpi": 130, "font.size": 9, "axes.grid": True, "grid.alpha": 0.3})


def noise_of(sc: str) -> dict:
    d = SCENARIOS[sc]
    return dict(q_rw=d.get("lo_rw_per_sqrt_s", 0.0), lin=d.get("lo_rel_per_s", 0.0), sin_amp=d.get("lo_sin_amp", 0.0),
                sin_period_s=d.get("lo_sin_period_s", 1200.0), gain_rw=d.get("gain_rw_per_sqrt_s", 0.0))


def cfg_for(blocks, sc="keine", R=1000.0, seed=1) -> SimConfig:
    return SimConfig.from_dict({**scenario_overrides(sc), "blocks": blocks, "source": {"R_herald": R},
                                "session": {"seed": seed}})


def fig_statistics(out: Path, quick: bool) -> dict:
    """σ(η̂) gegen N_A für verschiedene N_V (Formel) mit Punkten aus Simulatorläufen; c_W für W(0,0)."""
    res = {}
    NA = np.geomspace(1e4, 3e6, 60)
    fig, ax = plt.subplots(1, 2, figsize=(9, 3.4))
    for NV, c in ((5e4, "C3"), (2e5, "C1"), (6e5, "C0"), (np.inf, "k")):
        ax[0].loglog(NA, [sigma_eta(0.7, n, NV) for n in NA], c, label=f"N_V = {'∞' if np.isinf(NV) else f'{NV:,.0f}'.replace(',', ' ')}")
    # Simulatorpunkte: Sitzungen ohne Drift, Streuung von η̂ über Wiederholungen (N_V = 2·T_KV·10 kHz)
    pts = []
    for T_A in ((5, 20) if quick else (5, 20, 60)):
        bl = make_plan(PlanSpec(T_A=T_A, T_B=5, n_cycles=3, kv_every=0, T_KV=10, rate_KV=10000, T_KH=0))
        cfg = cfg_for(bl)
        mc = monte_carlo(cfg, n_rep=20 if quick else 200, modes=("pooled",), seed0=500)
        st = statistics(bl)
        pts.append(dict(T_A=T_A, N_A=st["N"]["heralded"], N_V=st["N"]["vacuum"], sd=mc["pooled"]["sd_A"],
                        bias=mc["pooled"]["bias_A"], pred=sigma_eta(0.7 * 1000 / 1020, st["N"]["heralded"], st["N"]["vacuum"]),
                        n_rep=mc["pooled"]["n_rep"]))
    ax[0].plot([p["N_A"] for p in pts], [p["sd"] for p in pts], "o", mfc="none", c="C1", ms=7,
               label="Simulator (N_V ≈ 2·10⁵)")
    ax[0].axhline(0.005, c="gray", ls=":"); ax[0].text(1.1e4, 0.0053, "Ziel 0,005", color="gray")
    ax[0].set(xlabel="Herald-Segmente N_A", ylabel="σ(η̂)", title="Effizienz η (≙ n̄ von |1⟩ nach Verlust), η = 0,7")
    ax[0].legend(fontsize=7)
    cw = {e: w00_coefficient(e, n=20000, n_boot=20 if quick else 40) for e in (0.5, 0.7, 0.9)}
    for e, c in zip(cw, ("C3", "C0", "C2")):
        ax[1].loglog(NA, cw[e] / np.sqrt(NA), c, label=f"η = {e}: c_W = {cw[e]:.2f}")
    ax[1].axhline(0.002, c="gray", ls=":"); ax[1].axhline(0.001, c="gray", ls=":")
    ax[1].set(xlabel="Herald-Segmente N_A", ylabel="σ(W(0,0))", title="W(0,0): σ = c_W/√N_A (Bootstrap)")
    ax[1].legend(fontsize=7)
    fig.tight_layout(); fig.savefig(out / "f1_statistik.png"); plt.close(fig)
    res["points"] = pts
    res["c_W"] = cw
    res["N_for"] = {f"{s}": float(np.ceil((cw[0.7] / s) ** 2)) for s in (0.004, 0.002, 0.001)}
    m2, m4 = moments_fock1(0.7)
    res["N_A_for_sigma_eta"] = {f"{s}": float(np.ceil((m4 - m2 ** 2) / s ** 2)) for s in (0.01, 0.005, 0.002)}
    res["N_V_for_sigma_eta_vac"] = {f"{s}": float(np.ceil(2 * m2 ** 2 / s ** 2)) for s in (0.01, 0.005, 0.002)}
    return res


def table_rates() -> list:
    rows = []
    for R, rl in ((1e3, 5000), (1e4, 5000), (1e4, 2000), (1e5, 2000), (1e5, 1000)):
        pre = min(2000, rl * 2 // 5) // 8 * 8
        Ra = accepted_rate(R, record_length=rl, pretrigger=pre)
        cw = 0.66
        N = (cw / 0.002) ** 2
        rows.append(dict(R=R, record_length=rl, R_acc=Ra, loss=1 - Ra / R, t_W002_s=N / Ra, t_W001_s=4 * N / Ra,
                         MBps=Ra * 2 * rl * 1.04 / 1e6, GB_per_100k=1e5 * 2 * rl * 1.04 / 1e9))
    return rows


def fig_drift_spacing(out: Path, quick: bool) -> dict:
    """Driftfehler von η̂ gegen den K-V-Abstand (Sitzung 60 min, T_A 60 s, T_B 30 s); Linien Modell, Punkte MC."""
    res = {}
    n_cyc = 40
    ks = [1, 2, 4, 8, 20, 0]
    fig, axs = plt.subplots(1, 2, figsize=(9, 3.4), sharey=True)
    for ax, sc in zip(axs, ("typisch", "schlecht")):
        dr = dict(SCENARIOS[sc])
        res[sc] = {}
        for m in NORMS:
            L, e, emc = [], [], []
            for k in ks:
                bl = make_plan(PlanSpec(T_A=60, T_B=30, n_cycles=n_cyc, kv_every=k, T_KV=20, rate_KV=10000, T_KH=0))
                de = drift_error(bl, dr, m, 0.7, n_sub=4)
                Lk = (k if k else n_cyc) * 91.0
                L.append(Lk / 60); e.append(de["A"])
            res[sc][m] = dict(L_min=L, err=e)
            ax.loglog(L, np.maximum(e, 1e-6), "-", c=COL[m], label=LBL[m])
        # MC (schnelle Studie) für zwei Abstände, Fehler ohne Statistik: √(rms² − σ_stat² − σ_cal²)
        for k in ((2, 20) if quick else (1, 4, 20)):
            bl = make_plan(PlanSpec(T_A=60, T_B=30, n_cycles=n_cyc, kv_every=k, T_KV=20, rate_KV=10000, T_KH=0))
            cfg = cfg_for(bl, sc, R=1000.0)
            mc = monte_carlo(cfg, n_rep=8 if quick else 24, modes=NORMS, seed0=900)
            st = statistics(bl)
            for m in NORMS:
                de = drift_error(bl, dr, m, 0.7, n_sub=4)
                ex2 = mc[m]["rms_A"] ** 2 - st["sigma_eta_stat_A"] ** 2 - de["cal_stat_A"] ** 2
                res[sc].setdefault("mc", []).append(dict(k=k, mode=m, rms=mc[m]["rms_A"], excess=float(np.sqrt(max(ex2, 0))),
                                                         model=de["A"], stat=st["sigma_eta_stat_A"], cal=de["cal_stat_A"],
                                                         model_total=float(np.sqrt(st["sigma_eta_stat_A"] ** 2
                                                                                   + de["cal_stat_A"] ** 2 + de["A"] ** 2)),
                                                         n_rep=mc[m]["n_rep"]))
        ax.axhline(0.005 * np.sqrt(1 / 3), c="gray", ls=":")
        ax.set_xticks([1.5, 3, 6, 12, 30, 60])
        ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:g}"))
        ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        ax.set(title=f"Szenario „{sc}“ [Annahme]", xlabel="K-V-Abstand (min)")
        ax.set_ylim(1e-5, 3e-2)
    axs[0].set_ylabel("Driftfehler von η̂ (A-Läufe)")
    axs[0].legend(fontsize=7, title="Normierung (Modell; MC-Prüfung s. Tabelle)", title_fontsize=7)
    axs[1].text(1.6, 0.0032, "Driftbudget 0,0029", color="gray", fontsize=7)
    fig.tight_layout(); fig.savefig(out / "f3_drift_kv_abstand.png"); plt.close(fig)
    return res


def fig_allan(out: Path) -> dict:
    """Allan-Abweichung simulierter LO-Pfade (4 h) und der statistische Boden einer K-V-Messung."""
    res = {}
    fig, ax = plt.subplots(figsize=(6.2, 3.6))
    dt = 1.0
    taus = None
    for sc, c in (("gut", "C2"), ("typisch", "C0"), ("schlecht", "C3")):
        ov = scenario_overrides(sc)
        cfg = SimConfig.from_dict({**ov, "drift": {**ov["drift"], "grid_dt_s": 1.0}})
        ys = []
        for s in range(8):
            p = DriftPath(cfg.drift, cfg.lo_monitor, 0.0, 4 * 3600.0, np.random.default_rng(40 + s))
            t = np.arange(0, 4 * 3600.0, dt)
            ys.append(p.lo(t))
        ads = []
        for y in ys:
            taus, ad, n = allan_deviation(y / y.mean(), dt)
            ads.append(ad)
        ad = np.sqrt(np.mean(np.square(ads), axis=0))
        fit = fit_noise_model(taus, ad, n)
        res[sc] = dict(tau=list(map(float, taus)), adev=list(map(float, ad)), fit=fit)
        ax.loglog(taus, ad, "o-", c=c, ms=3, label=f"LO „{sc}“")
    tt = np.geomspace(1, 1e4, 50)
    for rate, ls in ((1e4, "--"), (1e3, ":")):
        ax.loglog(tt, np.sqrt(2 / (rate * tt)), "k" + ls, lw=1,
                  label=f"Boden Vakuumvarianz, {rate:g} Seg./s")
    ax.set(xlabel="τ (s)", ylabel="Allan-Abweichung (relativ)",
           title="Stabilitätsmessung: Drift dominiert rechts vom Schnittpunkt")
    ax.legend(fontsize=7)
    fig.tight_layout(); fig.savefig(out / "f4_allan.png"); plt.close(fig)
    # Schnittpunkte (Vakuumvarianz ~ P_LO: relative Skala)
    for sc in res:
        ta, a = np.array(res[sc]["tau"]), np.array(res[sc]["adev"])
        floor = np.sqrt(2 / (1e4 * ta))
        i = np.nonzero(a > floor)[0]
        res[sc]["tau_cross_s_10kHz"] = float(ta[i[0]]) if i.size else None
    return res


def fig_aba(out: Path, quick: bool) -> dict:
    """ABA-Nachweis einer linearen η-Änderung (Justage) über die Sitzung; Fehlalarmrate ohne Änderung."""
    res = {}
    dtot = [0.0, 0.01, 0.02, 0.03, 0.04, 0.06]
    fig, ax = plt.subplots(figsize=(6.2, 3.4))
    for nc, c in ((3, "C1"), (6, "C0"), (12, "C2")):
        bl = make_plan(PlanSpec(T_A=60, T_B=30, n_cycles=nc, kv_every=3, T_KV=20, rate_KV=10000, T_KH=0))
        a_runs = [b for b in timeline(bl) if b["kind"] == "heralded"]
        span = a_runs[-1]["t_mid"] - a_runs[0]["t_mid"]
        fl = []
        for d in dtot:
            cfg = SimConfig.from_dict({"drift": {"eta_rel_per_s": d / span}, "blocks": bl, "session": {"seed": 1}})
            mc = monte_carlo(cfg, n_rep=20 if quick else 100, modes=("interpolated",), seed0=3000)
            fl.append(mc["interpolated"]["aba_flag"])
        res[f"n{nc}"] = dict(delta_eta_rel=dtot, flag=fl, session_min=statistics(bl)["t_total_s"] / 60,
                             n_rep=20 if quick else 100)
        ax.plot(np.array(dtot) * 100, fl, "o-", c=c, label=f"{nc} Zyklen ({res[f'n{nc}']['session_min']:.0f} min)")
    ax.axhline(0.0027, c="gray", ls=":")
    ax.set(xlabel="Änderung von η zwischen erstem und letztem A-Lauf (%, relativ)",
           ylabel="Anteil Sitzungen mit |z_ABA| > 3",
           title="ABA-Prüfung (A-Läufe je 60 s, 1 kHz Herald), Simulator")
    ax.legend(fontsize=7)
    fig.tight_layout(); fig.savefig(out / "f5_aba.png"); plt.close(fig)
    return res


def fig_engine(out: Path, quick: bool, amp: float = 10.0) -> dict:
    """Zeitraffer mit der vollen Engine (Belastungstest): Laufplan ×1/20, Drift „schlecht“ ×20 beschleunigt und
    in der Amplitude ×``amp`` verstärkt, damit der Normierungsfehler je Lauf über der Statistik liegt."""
    from hhd.sim.check import check_run
    from hhd.sim.engine import run
    f = 20.0
    d = dict(SCENARIOS["schlecht"])
    g = d.pop("gain_rw_per_sqrt_s")
    dz = dict(lo_rw_per_sqrt_s=amp * d["lo_rw_per_sqrt_s"] * np.sqrt(f), lo_sin_amp=amp * d["lo_sin_amp"],
              lo_sin_period_s=d["lo_sin_period_s"] / f, lo_rel_per_s=amp * d["lo_rel_per_s"] * f,
              eta_rw_per_sqrt_s=d["eta_rw_per_sqrt_s"] * np.sqrt(f), eta_sin_amp=d["eta_sin_amp"],
              eta_sin_period_s=d["eta_sin_period_s"] / f, grid_dt_s=0.01)
    sp = PlanSpec(T_A=3.0, T_B=1.5, n_cycles=6, kv_every=3, T_KV=1.0, rate_KV=10000, rate_B=2000, T_KH=1.0)
    if quick:
        sp = PlanSpec(T_A=1.0, T_B=0.5, n_cycles=4, kv_every=2, T_KV=0.5, rate_KV=10000, rate_B=2000, T_KH=0.5)
    bl = make_plan(sp)
    cfg = SimConfig.from_dict({"drift": dz, "lo_monitor": {"gain_rw_per_sqrt_s": amp * g * np.sqrt(f)}, "blocks": bl,
                               "source": {"R_herald": 4000.0}, "session": {"seed": 2026, "id": "zeitraffer"}})
    tmp = Path(tempfile.mkdtemp(prefix="hhd_zeitraffer_"))
    t0 = time.time()
    rr = run(cfg, tmp)
    t_run = time.time() - t0
    res = dict(plan=sp.to_dict(), drift=dz, amp=amp, compression=f, run_dir=str(tmp), t_engine_s=t_run, modes={})
    from hhd.sim.check import SessionReader
    rd = SessionReader(tmp)
    tm = {b: rd.t_mid(b) for b in rd.blocks()}
    tg = rd.tru["drift/t_grid"][:]; lo = rd.tru["drift/lo"][:]; ef = rd.tru["drift/eta_factor"][:]
    rd.close()
    fig, ax = plt.subplots(2, 1, figsize=(7.5, 5.2), sharex=True, gridspec_kw=dict(height_ratios=(1, 2)))
    ax[0].plot(tg, lo, c="k", lw=1, label="LO-Leistung (relativ)")
    ax[0].plot(tg, ef, c="C4", lw=1, label="η-Faktor")
    for b in tm:
        if b.startswith("kv"):
            ax[0].axvline(tm[b], c="C0", alpha=0.4)
    ax[0].set(ylabel="Wahrheit (relativ)"); ax[0].legend(fontsize=7, loc="best")
    ax[0].text(0.01, 0.04, "blau: K-V-Läufe", transform=ax[0].transAxes, fontsize=7, color="C0")
    for k, m in enumerate(NORMS):
        r = check_run(tmp, n_boot=5, norm_mode=m)
        dd = []
        for b, v in r["blocks"].items():
            if not (b.startswith("a_") or b.startswith("b_")) or "var_x" not in v.get("expected", {}):
                continue
            dd.append((tm[b], v["var_x"] - v["expected"]["var_x"], v["var_err"], b))
        dd.sort()
        x = np.array([q[0] for q in dd]) + (k - 1) * 0.25
        ax[1].errorbar(x, [q[1] for q in dd], [q[2] for q in dd], fmt="o", ms=3, c=COL[m], label=LBL[m], capsize=1.5)
        h = r["comparison"]["heralded"]
        res["modes"][m] = dict(runs={q[3]: dict(t=q[0], dev=q[1], err=q[2]) for q in dd},
                               A_dev=h["var_x"] - h["expected"]["var_x"], A_err=h["var_err"],
                               flags=r["comparison"]["drift"]["flags"],
                               aba_z=r["comparison"]["drift"].get("aba", {}).get("var_x", {}).get("z"))
    ax[1].axhline(0, c="gray")
    ax[1].set(xlabel="Sitzungszeit im Zeitraffer (s)  [×20 ≙ reale Zeit]",
              ylabel="Var(x) − Soll je A-/B-Lauf", title="")
    ax[1].legend(fontsize=7)
    ax[0].set_title(f"Volle Engine (Belastungstest): Laufplan ×1/20, Drift „schlecht“ ×20 schneller, ×{amp:g} stärker "
                    f"(Seed 2026, {t_run:.0f} s Rechenzeit)",
                    fontsize=8)
    fig.tight_layout(); fig.savefig(out / "f6_engine_zeitraffer.png"); plt.close(fig)
    return res


def table_adapt() -> list:
    rows = []
    for R, rl in ((1e3, 5000), (1e4, 2000), (1e5, 1000)):
        for sc in ("gut", "typisch", "schlecht"):
            for m in ("interpolated", "monitor"):
                r = adapt_plan(noise_of(sc), R_herald=R, record_length=rl, mode=m)
                st = r["statistics"]
                rows.append(dict(R=R, record_length=rl, scenario=sc, mode=m, spec=r["spec"], L_max_min=r["L_max_s"] / 60,
                                 t_min=st["t_total_s"] / 60, GB=st["data_GB"], sigma_eta=r["sigma_eta_total"],
                                 drift=r["drift_error"]["A"], sigma_W=r["sigma_W00"], feasible=r["feasible"], hint=r["hint"]))
    return rows


def default_plan_eval(quick: bool) -> dict:
    res = {}
    for R, rl in ((1e3, 5000), (1e4, 2000), (1e5, 1000)):
        bl = make_plan(DEFAULT)
        st = statistics(bl, R_herald=R, record_length=rl, pretrigger=min(2000, rl * 2 // 5) // 8 * 8)
        res[f"{R:g}"] = dict(stat=st)
    bl = make_plan(DEFAULT)
    for sc in ("gut", "typisch", "schlecht"):
        de = {m: drift_error(bl, SCENARIOS[sc], m, 0.7) for m in NORMS}
        mc = monte_carlo(cfg_for(bl, sc, 1000.0), n_rep=10 if quick else 40, modes=NORMS, seed0=7000)
        res[sc] = {m: dict(model=de[m]["A"], cal=de[m]["cal_stat_A"], rms=mc[m]["rms_A"], bias=mc[m]["bias_A"],
                           aba_flag=mc[m]["aba_flag"], kv_flag=mc[m]["kv_flag"], rms_delta=mc[m]["rms_delta"])
                   for m in NORMS}
    return res


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--skip-engine", action="store_true")
    a = ap.parse_args(argv)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    R = {}
    for name, fn in (("statistik", lambda: fig_statistics(out, a.quick)), ("raten", table_rates),
                     ("allan", lambda: fig_allan(out)), ("drift_abstand", lambda: fig_drift_spacing(out, a.quick)),
                     ("aba", lambda: fig_aba(out, a.quick)), ("default", lambda: default_plan_eval(a.quick)),
                     ("adapt", table_adapt)):
        t = time.time(); R[name] = fn(); print(f"{name}: {time.time() - t:.1f} s", flush=True)
    if not a.skip_engine:
        t = time.time(); R["engine"] = fig_engine(out, a.quick); print(f"engine: {time.time() - t:.1f} s")
    (out / "studie.json").write_text(json.dumps(R, indent=1, default=float), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
