"""Kampagnenstudie der Auswertung: Pulls, Bias, Abdeckung der Konfidenzintervalle je Methode, Driftindikatoren,
Nulltests (OP-5) über viele Sitzungen.

Je Sitzung: Synchronisation (falls fehlend), Auswertung mit gemeinsamem Vakuum-Bootstrap, optional zum Vergleich
ohne Vakuumnormierung im Bootstrap (``--old``), Validierung gegen truth.h5.

Aufruf: python tools/campaign_analysis.py <kampagne[,kampagne…]> [--n-boot 200] [--procs 4] [--old]
        [--fast] (nur Driftindikatoren: ohne Bootstrap und PCA) > studie.json
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "1"); os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np  # noqa: E402

from hhd.analysis.pipeline import AnalysisParams, analyze_session  # noqa: E402
from hhd.sim.analysis_validate import summarize_pulls, validate_analysis  # noqa: E402


def indicators(r: dict) -> dict:
    """Driftindikatoren und Nulltests einer Sitzung (für OP-5/OP-6)."""
    o = {}
    dv = r.get("drift_vacuum_lo", {})
    if "vacuum" in dv:
        v = dv["vacuum"]
        o["kv_z"] = v["z"]
        o["kv_slope_z"] = v["slope_rel_per_s"] / v["slope_err"] if v.get("slope_err") else None
    if "lo_monitor" in dv:
        o["lo_dev"] = dv["lo_monitor"]["max_rel_dev"]
    d = r.get("drift_AB", {})
    if "A_halves" in d:
        o["halves_z"] = d["A_halves"]["z"]
    if "ABA_chi2" in d:
        o["aba_max_z"] = d["ABA_chi2"]["max_abs_z"]
        from scipy.stats import chi2
        o["aba_chi2_p"] = float(chi2.sf(d["ABA_chi2"]["chi2"], d["ABA_chi2"]["ndof"]))
    for k in ("A", "B"):
        c = d.get(f"chi2_runs_{k}")
        if c:
            from scipy.stats import chi2
            o[f"runs_{k}_chi2_p"] = float(chi2.sf(c["chi2"], c["ndof"]))
    xt = r.get("crosstalk", {})
    if xt.get("available"):
        o["kh_z_var"] = xt["z_var"]; o["kh_z_mean"] = xt["z_mean"]; o["kh_wave_max_z"] = xt["waveform"]["max_abs_z"]
    cp = r.get("comparison")
    if cp:
        o["AB_ks_p"] = cp["ks"]["pvalue"]
        for k, v in cp.get("null_tests", {}).items():
            o[f"{k}_ks_p"] = v["ks_p"]; o[f"{k}_var_z"] = v["var_z"]
    return o


def one(args):
    d, a = args
    t = time.time()
    ap = AnalysisParams(n_boot=a["n_boot"])
    if a["fast"]:
        ap = replace(ap, n_boot=0, pca=False)
    r = analyze_session(d, ap, write=not a["fast"])
    v = validate_analysis(d, r) if not a["fast"] else None
    t1 = time.time() - t
    out = dict(dir=str(d), time_s=t1, perf=r["performance"], v=v, ind=indicators(r), warnings=r["warnings"],
               eta_el=r["calibration"].get("eta_el"), eta_el_err=r["calibration"].get("eta_el_err"))
    if r.get("pca"):
        out["pca"] = {k: r["pca"][k] for k in ("overlap_param", "overlap_noise_expected", "overlap_param_corrected",
                                              "tau_fit_ns", "centre_shift_ns")}
    if a["old"]:
        r0 = analyze_session(d, replace(ap, boot_vacuum=False, pca=False, jackknife_groups=0), write=False)
        out["v_novac"] = validate_analysis(d, r0)
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("camps"); p.add_argument("--n-boot", type=int, default=200); p.add_argument("--procs", type=int, default=4)
    p.add_argument("--old", action="store_true"); p.add_argument("--fast", action="store_true")
    a = p.parse_args()
    camps = [Path(c) for c in a.camps.split(",")]
    dirs = [q.parent for camp in camps for q in sorted(camp.glob("*/session.json"))]
    t0 = time.time()
    opts = dict(n_boot=a.n_boot, old=a.old, fast=a.fast)
    with ProcessPoolExecutor(a.procs) as ex:
        res = list(ex.map(one, [(d, opts) for d in dirs]))
    out = dict(campaign=[str(c) for c in camps], n_sessions=len(res), n_boot=a.n_boot, fast=a.fast,
               elapsed_s=time.time() - t0, time_per_session_s=float(np.mean([r["time_s"] for r in res])),
               n_warnings=int(sum(len(r["warnings"]) for r in res)),
               warnings=sorted({w.split(" (")[0] for r in res for w in r["warnings"]}))
    if not a.fast:
        out["summary"] = summarize_pulls([r["v"] for r in res])
        if a.old:
            out["summary_without_vacuum_bootstrap"] = summarize_pulls([r["v_novac"] for r in res])
        pca = [r["pca"] for r in res if r.get("pca")]
        if pca:
            out["pca"] = {k: float(np.mean([q[k] for q in pca])) for k in pca[0]}
    out["sessions"] = res
    print(json.dumps(out, indent=1, ensure_ascii=False, default=float))


if __name__ == "__main__":
    main()
