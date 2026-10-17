"""OP-6: Kalibrierung der Driftschwellen aus Kampagnen ohne Drift (Fehlalarm) und mit eingebrachter Drift (Erkennung).

Eingang: Ausgaben von ``tools/campaign_analysis.py`` (Feld ``ind`` je Sitzung). Je Indikator: Nullverteilung,
Schwelle für eine Fehlalarmrate α je Sitzung (Standard 1 %; aus der Normalverteilung, mit der Nullkampagne
geprüft) und Erkennungsrate je Driftszenario (Sitzungsname enthält das Szenario).

Aufruf: python tools/drift_thresholds.py null.json drift.json [alpha] [zeitfaktor] > op6.json

Zusätzlich je z-Indikator und Szenario der mittlere vorzeichenbehaftete z-Wert (Empfindlichkeit) und die daraus
hochgerechnete Erkennungsrate für Sitzungen mit ``zeitfaktor``-fach mehr Daten (Standard 90: M1-Laufplan in
Echtzeit statt Zeitraffer 1/90; z skaliert mit √Datenmenge): P(|N(μ·√f, 1)| > Schwelle).
"""
import json
import sys
from collections import defaultdict

import numpy as np
from scipy.stats import chi2, ncx2, norm

Z_IND = ("kv_z", "kv_slope_z", "halves_z", "kh_z_var", "kh_z_mean", "AA_var_z", "BB_var_z")
P_IND = ("aba_chi2_p", "runs_A_chi2_p", "runs_B_chi2_p", "AA_ks_p", "BB_ks_p")
CHI2_NDOF = {"aba_chi2_p": 6, "runs_A_chi2_p": 6, "runs_B_chi2_p": 5}     # M1: 7 A, 6 B, 6 A-B-A-Paare


def stat(ind, k):
    v = ind.get(k)
    return None if v is None else (abs(v) if k in Z_IND or k == "lo_dev" else v)


def main():
    null = json.load(open(sys.argv[1])); drift = json.load(open(sys.argv[2]))
    alpha = float(sys.argv[3]) if len(sys.argv) > 3 else 0.01
    tf = float(sys.argv[4]) if len(sys.argv) > 4 else 90.0
    signed = {k: (lambda i, k=k: i.get(k)) for k in Z_IND}
    N = [s["ind"] for s in null["sessions"]]
    scen = defaultdict(list)
    for s in drift["sessions"]:
        name = s["dir"].rstrip("/").split("/")[-2]
        scen[name].append(s["ind"])
    out = dict(alpha=alpha, time_factor=tf, n_null=len(N), scenarios={k: len(v) for k, v in scen.items()}, indicators={})
    # ABA max |z| über n Paare: Bonferroni-Schwelle; lo_dev: aus der Nullverteilung (Monitorrauschen)
    n_aba = 6
    for k in Z_IND + P_IND + ("aba_max_z", "lo_dev"):
        v0 = np.array([stat(i, k) for i in N if stat(i, k) is not None], float)
        if len(v0) == 0:
            continue
        if k in Z_IND:
            thr = float(norm.ppf(1 - alpha / 2)); fa = float(np.mean(v0 > thr)); rule = f"|z| > {thr:.2f}"
            fa3 = float(np.mean(v0 > 3.0))
            alarm = lambda x, t=thr: x > t
        elif k in P_IND:
            thr = alpha; fa = float(np.mean(v0 < thr)); rule = f"p < {thr:g}"; fa3 = float(np.mean(v0 < 0.0027))
            alarm = lambda x, t=thr: x < t
        elif k == "aba_max_z":
            thr = float(norm.ppf(1 - alpha / (2 * n_aba))); fa = float(np.mean(v0 > thr)); rule = f"max |z| > {thr:.2f}"
            fa3 = float(np.mean(v0 > 3.5))
            alarm = lambda x, t=thr: x > t
        else:   # lo_dev: relative Abweichung; Schwelle = max(5 × 99%-Quantil der Nullverteilung, 0,2 %)
            q = float(np.quantile(v0, 0.99)); thr = max(5 * q, 0.002); fa = float(np.mean(v0 > thr))
            rule = f"|ΔP/P| > {thr:.2%}"; fa3 = float(np.mean(v0 > 0.01))
            alarm = lambda x, t=thr: x > t
        det = {}
        for sc, inds in scen.items():
            vv = np.array([stat(i, k) for i in inds if stat(i, k) is not None], float)
            if len(vv):
                det[sc] = float(np.mean([alarm(x) for x in vv]))
        sens = {}
        if k in Z_IND:
            for sc, inds in scen.items():
                vv = np.array([i[k] for i in inds if i.get(k) is not None], float)
                v0s = np.array([i[k] for i in N if i.get(k) is not None], float)
                if len(vv):
                    mu = float(np.mean(vv) - np.mean(v0s))
                    m = mu * np.sqrt(tf)
                    sens[sc] = dict(mean_z=mu, mean_z_err=float(np.std(vv, ddof=1) / np.sqrt(len(vv))),
                                    detection_realtime=float(norm.sf(thr - m) + norm.cdf(-thr - m)))
        if k in CHI2_NDOF:                     # Nichtzentralität λ aus dem mittleren χ², hochgerechnet ×f
            nd = CHI2_NDOF[k]
            c0 = np.mean([chi2.isf(max(i[k], 1e-300), nd) for i in N if i.get(k) is not None])
            crit = chi2.isf(thr, nd)
            for sc, inds in scen.items():
                cc = np.array([chi2.isf(max(i[k], 1e-300), nd) for i in inds if i.get(k) is not None])
                if len(cc):
                    lam = float(max(np.mean(cc) - c0, 0.0))
                    sens[sc] = dict(lambda_=lam, lambda_err=float(np.std(cc, ddof=1) / np.sqrt(len(cc))),
                                    detection_realtime=float(ncx2.sf(crit, nd, lam * tf)) if lam > 0 else float(thr))
        out["indicators"][k] = dict(sensitivity=sens, rule=rule, threshold=thr, false_alarm=fa, false_alarm_err=float(np.sqrt(alpha * (1 - alpha) / len(v0))),
                                    false_alarm_old=fa3, null_mean=float(np.mean(v0)), null_std=float(np.std(v0, ddof=1)),
                                    detection=det)
    # Kombination: Alarm, wenn irgendein Indikator anschlägt (Sitzungs-Fehlalarm)
    def any_alarm(i):
        r = []
        for k, d in out["indicators"].items():
            x = stat(i, k)
            if x is None:
                continue
            r.append(x < d["threshold"] if k in P_IND else x > d["threshold"])
        return any(r)
    out["session_false_alarm"] = float(np.mean([any_alarm(i) for i in N]))
    out["session_detection"] = {sc: float(np.mean([any_alarm(i) for i in v])) for sc, v in scen.items()}
    # Hochrechnung Echtzeit: beste z-Größe je Szenario
    out["realtime_best"] = {}
    for sc in scen:
        best = max(((k, d["sensitivity"][sc]["detection_realtime"]) for k, d in out["indicators"].items()
                    if sc in d.get("sensitivity", {})), key=lambda t: t[1], default=None)
        if best:
            out["realtime_best"][sc] = dict(indicator=best[0], detection=best[1])
    print(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
