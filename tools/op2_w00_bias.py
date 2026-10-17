"""OP-2: Bias von W(0,0) je Schätzer, Abschneidung, Iterationszahl und N (Quadraturebene, ohne Synchronisation).

Zustand wie Lauf A der Standardsitzung: ξ·(verlustbehaftetes |1⟩, η) + (1−ξ)·Vakuum mit η_eff = 0,617.
Aufruf: python tools/op2_w00_bias.py [n_rep] [N]  → JSON auf stdout.
"""
import json
import sys
import time

import numpy as np

sys.path.insert(0, ".")
from hhd.recon.core import reconstruct, wigner00  # noqa: E402
from hhd.sim.check import em_photon_number  # noqa: E402  (frühere Referenz: EM 400 Iterationen)


def sample(rng, n, eta_eff):
    x1 = np.sqrt(rng.gamma(1.5, 1.0, n)) * rng.choice([-1.0, 1.0], n)
    one = rng.random(n) < 1.0           # η_eff enthält ξ (Mischung mit Vakuum ist ein Verlustkanal)
    return np.where(one, np.sqrt(eta_eff) * x1, 0.0) + np.sqrt(1 - eta_eff) * rng.normal(0, np.sqrt(0.5), n)


def main():
    n_rep = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    N = int(sys.argv[2]) if len(sys.argv) > 2 else 5000
    eta = 0.6169
    W_true = (1 - 2 * eta) / np.pi
    rng = np.random.default_rng(2026)
    cases = {
        "em400_n10 (alt)": lambda x: wigner00(em_photon_number(x, 10, 400)),
        "em150_n10 (alter Bootstrap)": lambda x: wigner00(em_photon_number(x, 10, 150)),
        "em_konv_n10": lambda x: reconstruct(x, 10, "em", tol=1e-9).W00,
        "maxlik_n6": lambda x: reconstruct(x, 6, "maxlik", tol=1e-9).W00,
        "maxlik_n10": lambda x: reconstruct(x, 10, "maxlik", tol=1e-9).W00,
        "maxlik_n16": lambda x: reconstruct(x, 16, "maxlik", tol=1e-9).W00,
        "binned200_n10": lambda x: reconstruct(x, 10, "binned", tol=1e-9).W00,
        "binned40_n10": lambda x: reconstruct(x, 10, "binned", tol=1e-9, bins=40).W00,
        "moments_n2": lambda x: reconstruct(x, 2, "moments").W00,
        "moments_n3": lambda x: reconstruct(x, 3, "moments").W00,
    }
    res = {k: [] for k in cases}
    t0 = time.time()
    for _ in range(n_rep):
        x = sample(rng, N, eta)
        for k, f in cases.items():
            res[k].append(f(x))
    out = dict(N=N, n_rep=n_rep, W_true=W_true, time_s=time.time() - t0, cases={})
    for k, v in res.items():
        v = np.array(v)
        out["cases"][k] = dict(bias=float(v.mean() - W_true), sigma=float(v.std(ddof=1)),
                               bias_over_sigma=float((v.mean() - W_true) / v.std(ddof=1)),
                               bias_err=float(v.std(ddof=1) / np.sqrt(len(v))))
    print(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
