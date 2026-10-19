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
        T = dict(var_x=avg("var_x"), W00=avg("W00"))
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
    return out


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
