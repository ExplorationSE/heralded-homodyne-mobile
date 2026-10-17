"""Konfidenzintervalle aus dem Bootstrap (0.8.0): Perzentil, Basis, Normal, biaskorrigiert-normal, BCa, studentisiert.

* ``percentile``  Perzentile der Bootstrap-Verteilung θ*;
* ``basic``       2θ̂ − Perzentile (gespiegelt);
* ``normal``      θ̂ ± z·sd(θ*);
* ``bc_normal``   (θ̂ − b̂) ± z·sd(θ*) mit Bootstrap-Bias b̂ = ⟨θ*⟩ − θ̂;
* ``bca``         Efron-BCa: Biaskorrektur z₀ aus dem Anteil θ* < θ̂, Beschleunigung a aus einem
                  Gruppen-Jackknife (G Teilmengen der Records);
* ``studentized`` Bootstrap-t mit einem Standardfehler je Ziehung (analytisch bzw. Fisher):
                  [θ̂ − t*_{1−α/2}·se, θ̂ − t*_{α/2}·se], t* = (θ* − θ̂)/se*; se des Punktschätzers wird mit
                  sd(θ*)/⟨se*⟩ auf den Gesamtfehler (inkl. Vakuumnormierung) skaliert.
"""
from __future__ import annotations

import numpy as np
from scipy.stats import norm as _N

METHODS = ("percentile", "basic", "normal", "bc_normal", "bca", "studentized")
LEVELS = (0.68, 0.95)


def _q(v, p):
    return float(np.percentile(v, 100 * p))


def intervals(theta: float, boot, se_boot=None, se_point=None, jack=None) -> dict:
    v = np.asarray(boot, float)
    v = v[np.isfinite(v)]
    if len(v) < 5:
        return {}
    sd = float(np.std(v, ddof=1))
    bias = float(np.mean(v) - theta)
    out = {m: {} for m in METHODS}
    # BCa-Parameter
    frac = (np.sum(v < theta) + 0.5 * np.sum(v == theta)) / len(v)
    z0 = float(_N.ppf(np.clip(frac, 1 / (2 * len(v)), 1 - 1 / (2 * len(v)))))
    a = 0.0
    if jack is not None and len(jack) >= 3:
        j = np.asarray(jack, float)
        d = j.mean() - j
        den = 6 * (np.sum(d ** 2)) ** 1.5
        a = float(np.sum(d ** 3) / den) if den > 0 else 0.0
    for lev in LEVELS:
        al = (1 - lev) / 2
        z = float(_N.ppf(1 - al))
        key = f"{int(round(100 * lev))}"
        lo, hi = _q(v, al), _q(v, 1 - al)
        out["percentile"][key] = [lo, hi]
        out["basic"][key] = [2 * theta - hi, 2 * theta - lo]
        out["normal"][key] = [theta - z * sd, theta + z * sd]
        out["bc_normal"][key] = [theta - bias - z * sd, theta - bias + z * sd]
        adj = []
        for zz in (-z, z):
            w = z0 + (z0 + zz) / (1 - a * (z0 + zz))
            adj.append(float(_N.cdf(w)))
        out["bca"][key] = [_q(v, adj[0]), _q(v, adj[1])]
        if se_boot is not None and se_point is not None:
            sb = np.asarray(se_boot, float)[: len(boot)]
            ok = np.isfinite(sb) & (sb > 0) & np.isfinite(np.asarray(boot, float)[: len(sb)])
            if ok.sum() >= 5:
                t = (np.asarray(boot, float)[: len(sb)][ok] - theta) / sb[ok]
                scale = sd / float(np.mean(sb[ok])) if np.mean(sb[ok]) > 0 else 1.0
                se = se_point * scale
                out["studentized"][key] = [theta - _q(t, 1 - al) * se, theta - _q(t, al) * se]
    if not out["studentized"]:
        out.pop("studentized")
    out["_params"] = dict(z0=z0, a=a, bias=bias, sd=sd)
    return out


def jackknife_groups(n: int, G: int, rng) -> list[np.ndarray]:
    """G disjunkte Teilmengen (zufällige Zuordnung) – je Jackknife-Schritt wird eine weggelassen."""
    lab = rng.permutation(n) % G
    return [lab != g for g in range(G)]
