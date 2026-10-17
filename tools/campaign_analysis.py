"""Kampagnenstudie der Auswertung: Pulls, Bias und Abdeckung der Konfidenzintervalle (≥ 20 Sitzungen).

Je Sitzung: Synchronisation (falls fehlend), Auswertung mit gemeinsamem Vakuum-Bootstrap (OP-1-Lösung) und zum
Vergleich ohne Vakuumnormierung im Bootstrap (frühere Methode), Validierung gegen truth.h5.

Aufruf: python tools/campaign_analysis.py <kampagne> [n_boot] [n_proc] > studie.json
"""
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "1"); os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hhd.analysis.pipeline import AnalysisParams, analyze_session  # noqa: E402
from hhd.sim.analysis_validate import summarize_pulls, validate_analysis  # noqa: E402


def one(args):
    d, n_boot = args
    t = time.time()
    ap = AnalysisParams(n_boot=n_boot)
    r = analyze_session(d, ap)
    v = validate_analysis(d, r)
    t1 = time.time() - t
    r0 = analyze_session(d, replace(ap, boot_vacuum=False, pca=False), write=False)
    v0 = validate_analysis(d, r0)
    return dict(dir=str(d), time_s=t1, perf=r["performance"], v=v, v_novac=v0,
                pca=dict((k, r["pca"][k]) for k in ("overlap_param", "overlap_noise_expected", "overlap_param_corrected",
                                                   "tau_fit_ns", "centre_shift_ns")) if r.get("pca") else None,
                warnings=r["warnings"])


def main():
    camp = Path(sys.argv[1])
    n_boot = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    n_proc = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    dirs = sorted(p.parent for p in camp.glob("*/session.json"))
    t0 = time.time()
    with ProcessPoolExecutor(n_proc) as ex:
        res = list(ex.map(one, [(d, n_boot) for d in dirs]))
    import numpy as np
    pca = [r["pca"] for r in res if r["pca"]]
    out = dict(campaign=str(camp), n_sessions=len(res), n_boot=n_boot, elapsed_s=time.time() - t0,
               summary=summarize_pulls([r["v"] for r in res]),
               summary_without_vacuum_bootstrap=summarize_pulls([r["v_novac"] for r in res]),
               pca=dict(overlap_mean=float(np.mean([p["overlap_param"] for p in pca])),
                        overlap_corrected_mean=float(np.mean([p["overlap_param_corrected"] for p in pca])),
                        overlap_corrected_std=float(np.std([p["overlap_param_corrected"] for p in pca], ddof=1)),
                        tau_fit_mean_ns=float(np.mean([p["tau_fit_ns"] for p in pca])),
                        centre_shift_mean_ns=float(np.mean([p["centre_shift_ns"] for p in pca]))) if pca else None,
               time_per_session_s=float(np.mean([r["time_s"] for r in res])),
               n_warnings=int(sum(len(r["warnings"]) for r in res)),
               warnings=sorted({w for r in res for w in r["warnings"]}),
               sessions=res)
    print(json.dumps(out, indent=1, ensure_ascii=False, default=float))


if __name__ == "__main__":
    main()
