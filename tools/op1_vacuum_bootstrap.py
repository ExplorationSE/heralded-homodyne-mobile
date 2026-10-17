"""OP-1: η-Fehler mit/ohne Vakuumnormierung im Bootstrap, abhängig von der Zahl der Vakuum-Records (Quadraturebene).

Rohquadraturen in LSB-Einheiten: K-V q_V ~ N(0, s²), A q_A = s·√2·x_A (x_A verlustbehaftetes |1⟩, η_eff).
Normierung x = q·√(½/Var(q_V)). Je Wiederholung: η (Fit n ≤ 1 und Var − ½), Bootstrap-Fehler (a) nur A neu gezogen
(alte Methode), (b) A und K-V gemeinsam neu gezogen. Ergebnis: Pull-Breiten.
Aufruf: python tools/op1_vacuum_bootstrap.py [n_rep] [N_A] [n_boot] > op1.json
"""
import json
import sys
import time

import numpy as np

sys.path.insert(0, ".")
from hhd.recon.core import fit_eta_fock1  # noqa: E402


def lossy1(rng, n, eta):
    x1 = np.sqrt(rng.gamma(1.5, 1.0, n)) * rng.choice([-1.0, 1.0], n)
    return np.sqrt(eta) * x1 + np.sqrt(1 - eta) * rng.normal(0, np.sqrt(0.5), n)


def est(qa, qv):
    x = qa * np.sqrt(0.5 / np.mean(qv ** 2))
    return fit_eta_fock1(x)["eta"], float(np.var(x)) - 0.5


def main():
    n_rep = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    NA = int(sys.argv[2]) if len(sys.argv) > 2 else 5000
    nb = int(sys.argv[3]) if len(sys.argv) > 3 else 100
    eta, s = 0.6169, 80.0
    rng = np.random.default_rng(11)
    out = dict(N_A=NA, n_rep=n_rep, n_boot=nb, eta_true=eta, cases={})
    t0 = time.time()
    for NV in (2000, 6600, 20000, 50000):
        rows = []
        for _ in range(n_rep):
            qa = s * lossy1(rng, NA, eta) / np.sqrt(0.5)
            qv = rng.normal(0, s, NV)
            e_fit, e_var = est(qa, qv)
            ba, bb = [], []
            for _ in range(nb):
                ia = rng.integers(0, NA, NA)
                ba.append(est(qa[ia], qv))
                bb.append(est(qa[ia], qv[rng.integers(0, NV, NV)]))
            ba, bb = np.array(ba), np.array(bb)
            rows.append([e_fit, e_var, *ba.std(0, ddof=1), *bb.std(0, ddof=1)])
        r = np.array(rows)
        c = {}
        for j, name in enumerate(("eta_fit", "eta_var")):
            d = r[:, j] - eta
            c[name] = dict(scatter=float(d.std(ddof=1)), bias=float(d.mean()),
                           err_A_only=float(r[:, 2 + j].mean()), err_joint=float(r[:, 4 + j].mean()),
                           pull_width_A_only=float((d / r[:, 2 + j]).std(ddof=1)),
                           pull_width_joint=float((d / r[:, 4 + j]).std(ddof=1)),
                           pull_width_err=float(1 / np.sqrt(2 * (n_rep - 1))))
        out["cases"][str(NV)] = c
    out["time_s"] = time.time() - t0
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
