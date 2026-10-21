"""Validierung der Auswertung (Kalibrierung, Rekonstruktion, Vergleich) gegen die Simulator-Wahrheit.

Nur dieses Werkzeug (und die Tests) lesen ``truth.h5``. Sollwerte je Gruppe: mit der Recordzahl gewichtetes Mittel
der Block-Sollwerte (``expected_json``). Pulls (Wert − Soll)/Fehler und Abdeckung der Bootstrap-Intervalle.

Hinweis zum Sollwert für A: ``eta_eff = ξ·η_state·η_el`` ohne ungeheraldete/sekundäre Moden und Überlappverluste;
die Auswertung verwirft Records mit sekundären Moden/Überlappung, sodass der Sollwert bis auf die Modenanpassung gilt.
"""
from __future__ import annotations

import json
from pathlib import Path

import h5py
import numpy as np


def _expected(session_dir: Path) -> dict:
    with h5py.File(session_dir / "truth.h5", "r") as f:
        return {k: json.loads(g.attrs["expected_json"]) for k, g in f["blocks"].items()}


def _pull(v, t, e):
    return float((v - t) / e) if (v is not None and e is not None and t is not None and e > 0) else None


def _in(ci, t):
    return bool(ci[0] <= t <= ci[1]) if ci else None


def validate_analysis(session_dir, result: dict) -> dict:
    d = Path(session_dir)
    exp = _expected(d)
    rec = result.get("reconstruction", {})
    out: dict = dict(session=str(d), session_id=result.get("session_id"), groups={})
    tru_group = {}
    preset = (result.get("state") or {}).get("preset", "fock1")
    fock1 = preset == "fock1"
    out["state"] = preset
    for k, info in result["groups"].items():
        if k not in rec:
            continue
        runs = info["runs"]
        g = rec[k]
        w = np.array([1.0 for _ in runs])
        def avg(q):
            vals = [exp[b].get(q) for b in runs]
            if any(v is None for v in vals):
                return None
            return float(np.average(vals, weights=w))
        T = dict(var_x=avg("var_x"), W00=avg("W00"), mandel_Q=avg("mandel_Q"))
        T["eta"] = avg("eta_eff") if k == "A" else (avg("n_eff") if k == "B" else 0.0)
        tru_group[k] = T
        o = dict(true=T, n=g["n"])
        qs = [("var_x", "var_x"), ("W00", "W00"), ("W00_moments", "W00"), ("eta_fit", "eta"), ("eta_var", "eta")]
        if k == "A" and not fock1:
            # anderer Zustand: |1⟩-Verlustfit und eta_var nicht anwendbar, stattdessen η = (Var x − ½)/⟨n⟩_ideal
            qs = [("var_x", "var_x"), ("W00", "W00"), ("W00_moments", "W00"), ("eta_n", "eta")]
            pn = [exp[b].get("p_n") for b in runs]
            if any(p is None or sum(p[3:]) > 1e-3 for p in pn):
                qs.remove(("W00_moments", "W00"))   # linearer Schätzer nur für p_n ≈ 0 ab n = 3
        for q, tq in qs:
            t = T.get(tq)
            if q not in g:
                continue
            if t is None:
                continue
            o[q] = dict(value=g[q], err=g.get(f"{q}_err"), true=t, diff=g[q] - t, pull=_pull(g[q], t, g.get(f"{q}_err")))
            if g.get(f"{q}_ci68"):
                o[q].update(in68=_in(g[f"{q}_ci68"], t), in95=_in(g[f"{q}_ci95"], t), ci_method=g.get(f"{q}_ci_method"))
            elif g.get(f"{q}_boot"):
                bs = g[f"{q}_boot"]
                o[q].update(in68=_in(bs["ci68"], t), in95=_in(bs["ci95"], t))
            ca = g.get("ci_all", {}).get(q)
            if ca:
                o[q]["cover"] = {m: dict(in68=_in(v["68"], t), in95=_in(v["95"], t),
                                         w68=v["68"][1] - v["68"][0]) for m, v in ca.items()}
        if g.get("mandel_Q") is not None and T.get("mandel_Q") is not None and np.isfinite(T["mandel_Q"]):
            o["mandel_Q"] = dict(value=g["mandel_Q"], err=g.get("mandel_Q_err"), true=T["mandel_Q"],
                                 diff=g["mandel_Q"] - T["mandel_Q"],
                                 pull=_pull(g["mandel_Q"], T["mandel_Q"], g.get("mandel_Q_err")))
        if "W00_bc" in g and T.get("W00") is not None:
            o["W00_bc"] = dict(value=g["W00_bc"], err=g.get("W00_err"), true=T["W00"], diff=g["W00_bc"] - T["W00"],
                               pull=_pull(g["W00_bc"], T["W00"], g.get("W00_err")))
        out["groups"][k] = o
    cp = result.get("comparison")
    if cp and "A" in tru_group and "B" in tru_group:
        t = tru_group["A"]["var_x"] - tru_group["B"]["var_x"]
        v = cp["diff_var_x"]
        out["eta_comparison"] = dict(value=v["value"], err=v["err"], true=t, pull=_pull(v["value"], t, v["err"]))
        tw = tru_group["A"]["W00"] - tru_group["B"]["W00"]
        v = cp["diff_W00"]
        out["W00_diff"] = dict(value=v["value"], err=v["err"], true=tw, pull=_pull(v["value"], tw, v["err"]))
    cal = result["calibration"]
    any_b = next(iter(exp.values()))
    if cal.get("eta_el") is not None:
        out["eta_el"] = dict(value=cal["eta_el"], true=any_b["eta_electronic"], diff=cal["eta_el"] - any_b["eta_electronic"])
    eb = result.get("efficiency_budget", {})
    xi_t = [exp[b]["xi"] for b in result["groups"].get("A", {}).get("runs", []) if "xi" in exp[b]]
    if xi_t and eb.get("xi") is not None:
        out["xi"] = dict(value=eb["xi"], true=float(np.mean(xi_t)), diff=eb["xi"] - float(np.mean(xi_t)))
    if "A" in rec and xi_t:
        es = [exp[b]["eta_state"] for b in result["groups"]["A"]["runs"]]
        if "eta_rest" in eb:
            # ohne bekanntes η_el enthält η_rest noch das elektronische Rauschen
            t = float(np.mean(es)) * (1.0 if cal.get("eta_el") is not None else any_b["eta_electronic"])
            out["eta_state"] = dict(value=eb["eta_rest"]["value"], err=eb["eta_rest"]["err"], true=t,
                                    pull=_pull(eb["eta_rest"]["value"], t, eb["eta_rest"]["err"]),
                                    eta_el_removed=cal.get("eta_el") is not None)
    ph = result.get("phase") or {}
    if ph.get("available"):
        try:
            out["phase"] = validate_phase(d, result, exp)
        except Exception as e:                       # Validierung darf die Auswertung nicht abbrechen
            out["phase"] = dict(error=f"{type(e).__name__}: {e}")
    return out


def _true_theta(d: Path, b: str):
    for fn in ("truth.h5", "truth_records.h5"):
        p = d / fn
        if p.exists():
            with h5py.File(p, "r") as f:
                k = f"blocks/{b}/records/theta_true" if fn == "truth.h5" else f"blocks/{b}/theta_true"
                if k in f:
                    return f[k][:].astype(float)
    return None


def validate_phase(d: Path, result: dict, exp: dict) -> dict:
    """θ-Kalibrierung gegen ``theta_true`` je Record; ρ gegen ρ_soll = ξ·L_η(ρ_t, dephasiert) + (1−ξ)|0⟩⟨0|."""
    from ..recon import tomo as T
    from ..sim.states import loss_channel, make_state
    ph = result["phase"]
    o: dict = {}
    pth = d / "analysis" / "phase.h5"
    if not pth.exists() and result.get("out_dir"):
        pth = Path(result["out_dir"]) / "phase.h5"
    errs = {}
    if pth.exists():
        with h5py.File(pth, "r") as f:
            for b in f["runs"]:
                tt = _true_theta(d, b)
                if tt is None:
                    continue
                th = f[f"runs/{b}/theta"][:].astype(float)
                ee = f[f"runs/{b}/theta_err"][:].astype(float)
                n = min(len(tt), len(th))
                m = np.isfinite(tt[:n]) & np.isfinite(th[:n])
                if m.sum() < 10:
                    continue
                dd = np.angle(np.exp(1j * (th[:n][m] - tt[:n][m])))
                errs[b] = dict(n=int(m.sum()), mean=float(dd.mean()), rms=float(np.sqrt(np.mean(dd ** 2))),
                               p95=float(np.percentile(np.abs(dd), 95)), sigma_reported=float(np.median(ee[:n][m])),
                               kind=f[f"runs/{b}"].attrs.get("dataset_type"))
    if errs:
        alld = [v for v in errs.values()]
        w = np.array([v["n"] for v in alld], float)
        o["theta"] = dict(runs=errs, rms=float(np.sqrt(np.average([v["rms"] ** 2 for v in alld], weights=w))),
                          bias=float(np.average([v["mean"] for v in alld], weights=w)),
                          sigma_reported=float(np.average([v["sigma_reported"] for v in alld], weights=w)))
    runs = result["groups"].get("A", {}).get("runs", [])
    ea = [exp[b] for b in runs if b in exp]
    if not ea or not ea[0].get("state_preset"):
        return o
    rt = make_state(ea[0]["state_preset"]).rho()
    eta_t = float(np.mean([e.get("eta_tomo", e.get("eta_state", 1.0)) for e in ea]))
    xi = float(np.mean([e.get("xi", 1.0) for e in ea]))
    # θ_true enthält das Phasenrauschen; ρ wird nur durch den Fehler der θ-Zuordnung dephasiert (gemessen gegen θ_true)
    ra = [v for bb, v in errs.items() if bb in runs]
    if ra:
        sig = float(np.sqrt(np.average([v["rms"] ** 2 - v["mean"] ** 2 for v in ra], weights=[v["n"] for v in ra])))
        sig_src = "theta_cal_vs_truth"
    else:
        sig, sig_src = float(np.mean([e.get("phase_noise_rad", 0.0) for e in ea])), "phase_noise_rad"
    rho_m = np.array(ph["rho"]["re"]) + 1j * np.array(ph["rho"]["im"])
    N = rho_m.shape[0]
    M = max(N, rt.shape[0])
    rt = np.pad(rt, ((0, M - rt.shape[0]), (0, M - rt.shape[0])))
    k = np.arange(M)
    lam = loss_channel(rt, eta_t) * np.exp(-0.5 * sig ** 2 * (k[:, None] - k[None, :]) ** 2)
    rex = xi * lam
    from ..sim.states import background_rho
    bgr = background_rho(result.get("state"), M)
    if bgr is None:
        rex[0, 0] += 1 - xi
    else:
        rex = rex + (1 - xi) * loss_channel(bgr, eta_t) * np.exp(-0.5 * sig ** 2 * (k[:, None] - k[None, :]) ** 2)
    rex = rex[:N, :N] / np.real(np.trace(rex[:N, :N]))
    me, mm = T.state_metrics(rex), ph["metrics"]
    bt = ph.get("boot", {})
    o["model"] = dict(eta_tomo=eta_t, xi=xi, dephasing_rad=sig, dephasing_source=sig_src)
    o["fidelity_expected"] = float(T.fidelity(rho_m, rex))
    o["fidelity_expected_maxrot"] = float(T.fidelity_max_rotation(rho_m, rex)[0])
    for q in ("W00", "parity", "mean_n", "squeezing_dB", "alpha_from_a2", "purity"):
        e = (bt.get(q) or {}).get("err")
        o[q] = dict(value=mm[q], true=me[q], diff=mm[q] - me[q], err=e, pull=_pull(mm[q], me[q], e))
    if me["squeezing_dB"] < -0.3:
        da = float(np.angle(np.exp(2j * (mm["axis_rad"] - me["axis_rad"]))) / 2)
        o["axis_rad"] = dict(value=mm["axis_rad"], true=me["axis_rad"], diff=da, err=(bt.get("axis_rad") or {}).get("err"))
    # Var(x_θ)-Fit am Detektor (A) gegen min_θ Var der Sollwerte (Mischung ξ/ohne Herald, η·η_el)
    vf = (ph.get("var_theta") or {}).get("fit") or {}
    sq_t = [e.get("squeezing_dB") for e in ea]
    if vf.get("available") and all(v is not None for v in sq_t):
        t = float(np.mean(sq_t))
        o["var_fit_dB"] = dict(value=vf["squeezing_dB"], err=vf.get("squeezing_dB_err"), true=t,
                               diff=vf["squeezing_dB"] - t, pull=_pull(vf["squeezing_dB"], t, vf.get("squeezing_dB_err")))
    o["B"] = validate_phase_B(result, exp, M, sig)
    return o


def validate_phase_B(result: dict, exp: dict, M: int, sig: float) -> dict:
    """Lauf B phasenaufgelöst: Var(x_θ)-Fit gegen die Soll-Quetschung (Detektor) und ρ gegen L_η(ρ_ohne Herald)."""
    from ..recon import tomo as T
    from ..sim.states import background_rho, loss_channel
    PB = (result.get("phase") or {}).get("B") or {}
    if not PB.get("available"):
        return dict(available=False, reason=PB.get("reason"))
    runs = result["groups"].get("B", {}).get("runs", [])
    eb = [exp[b] for b in runs if b in exp]
    o: dict = dict(available=True, n=PB.get("n_records"))
    vf = (PB.get("var_theta") or {}).get("fit") or {}
    sq_t = [e.get("squeezing_dB") for e in eb]
    if vf.get("available") and sq_t and all(v is not None for v in sq_t):
        t = float(np.mean(sq_t))
        o["var_fit_dB"] = dict(value=vf["squeezing_dB"], err=vf.get("squeezing_dB_err"), true=t,
                               diff=vf["squeezing_dB"] - t, pull=_pull(vf["squeezing_dB"], t, vf.get("squeezing_dB_err")))
    eta_t = float(np.mean([e.get("eta_state", 1.0) for e in eb])) if eb else 1.0
    rb = background_rho(result.get("state"), M)
    if rb is None:
        rb = np.zeros((M, M)); rb[0, 0] = 1.0
    k = np.arange(M)
    lam = loss_channel(rb, eta_t) * np.exp(-0.5 * sig ** 2 * (k[:, None] - k[None, :]) ** 2)
    rho_m_metrics = PB["metrics"]
    N = int(rho_m_metrics["N"])
    rex = lam[:N, :N] / np.real(np.trace(lam[:N, :N]))
    me, bt = T.state_metrics(rex), PB.get("boot", {})
    for q in ("W00", "mean_n", "squeezing_dB", "purity"):
        e = (bt.get(q) or {}).get("err")
        o[q] = dict(value=rho_m_metrics[q], true=me[q], diff=rho_m_metrics[q] - me[q], err=e,
                    pull=_pull(rho_m_metrics[q], me[q], e))
    return o


def summarize_pulls(vals: list[dict], keys=("A.var_x", "A.W00", "A.W00_bc", "A.W00_moments", "A.eta_fit", "A.eta_var", "A.eta_n",
                                           "B.var_x", "B.W00_moments", "eta_comparison", "W00_diff", "eta_state")) -> dict:
    """Kampagnenauswertung: Mittel/Streuung der Pulls, Bias (absolut und in σ), Abdeckung 68/95 %."""
    out = {}
    # B.W00 (MaxLik) und B.eta_fit liegen für Vakuum am Rand des Parameterraums: kein sinnvoller Pull
    for key in keys:
        parts = key.split(".")
        rows = []
        for v in vals:
            o = v["groups"].get(parts[0], {}).get(parts[1]) if len(parts) == 2 else v.get(key)
            if o and o.get("pull") is not None:
                rows.append(o)
        if not rows:
            continue
        p = np.array([r["pull"] for r in rows])
        dd = np.array([r.get("diff", r["value"] - r["true"]) for r in rows])
        e = np.array([r["err"] for r in rows])
        s = dict(n=len(p), pull_mean=float(p.mean()), pull_mean_err=float(p.std(ddof=1) / np.sqrt(len(p))) if len(p) > 1 else None,
                 pull_std=float(p.std(ddof=1)) if len(p) > 1 else None,
                 pull_std_err=float(p.std(ddof=1) / np.sqrt(2 * (len(p) - 1))) if len(p) > 1 else None,
                 bias=float(dd.mean()), bias_err=float(dd.std(ddof=1) / np.sqrt(len(dd))) if len(dd) > 1 else None,
                 bias_over_sigma=float(dd.mean() / np.mean(e)), mean_err=float(np.mean(e)),
                 scatter=float(dd.std(ddof=1)) if len(dd) > 1 else None)
        c68 = [r["in68"] for r in rows if r.get("in68") is not None]
        c95 = [r["in95"] for r in rows if r.get("in95") is not None]
        if c68:
            s["coverage68"] = float(np.mean(c68)); s["coverage95"] = float(np.mean(c95))
            s["coverage68_err"] = float(np.sqrt(0.68 * 0.32 / len(c68))); s["coverage95_err"] = float(np.sqrt(0.05 * 0.95 / len(c95)))
        meths = sorted({m for r in rows for m in r.get("cover", {})})
        if meths:
            s["coverage_by_method"] = {
                m: dict(n=len([r for r in rows if m in r.get("cover", {})]),
                        c68=float(np.mean([r["cover"][m]["in68"] for r in rows if m in r.get("cover", {})])),
                        c95=float(np.mean([r["cover"][m]["in95"] for r in rows if m in r.get("cover", {})])),
                        mean_w68=float(np.mean([r["cover"][m]["w68"] for r in rows if m in r.get("cover", {})])))
                for m in meths}
        out[key] = s
    return out
