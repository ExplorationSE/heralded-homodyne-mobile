"""Kenngrößen einer Quadraturgruppe (phasengemittelt) und lineare Schätzer mit analytischem Fehler."""
from __future__ import annotations

import numpy as np

from .core import (_moment_matrix, fidelity_diag, fit_eta_fock1, lossy_fock1, photon_stats, reconstruct,
                   wigner_radial)

R_GRID = np.linspace(0.0, 3.0, 31)


def w00_score_coeffs(N: int = 2) -> np.ndarray:
    """a_k mit W(0,0) = Σ_k a_k ⟨x^{2k}⟩ (Momentenschätzer, erwartungstreu für ρ_nn = 0, n > N)."""
    M = _moment_matrix(N, 1.0)
    w = (-1.0) ** np.arange(N + 1) / np.pi
    return w @ np.linalg.inv(M)


def w00_linear(x: np.ndarray, N: int = 2) -> tuple[float, float]:
    """W(0,0) und Standardfehler aus dem Momentenschätzer (Score je Record)."""
    a = w00_score_coeffs(N)
    s = sum(a[k] * x ** (2 * k) for k in range(N + 1))
    return float(np.mean(s)), float(np.std(s, ddof=1) / np.sqrt(len(s)))


def var_with_err(x: np.ndarray) -> tuple[float, float]:
    x = np.asarray(x, float)
    v = float(np.var(x))
    return v, float(np.std((x - x.mean()) ** 2, ddof=1) / np.sqrt(len(x)))


def group_stats(x: np.ndarray, nmax: int = 10, estimator: str = "maxlik", p0=None, full: bool = True,
                tol: float = 1e-8) -> dict:
    x = np.asarray(x, float)
    r = reconstruct(x, nmax, estimator, p0=p0, tol=tol)
    v, ve = var_with_err(x)
    wm, wme = w00_linear(x, 2)
    ef = fit_eta_fock1(x)
    out = dict(n=int(len(x)), mean_x=float(x.mean()), var_x=v, var_err=ve, W00=r.W00, W00_moments=wm,
               W00_moments_err=wme, p_n=r.p.tolist(), eta_fit=ef["eta"], eta_fit_err_fisher=ef["sigma_fisher"],
               eta_var=v - 0.5, n_iter=r.n_iter, converged=r.converged, estimator=estimator, nmax=nmax)
    if full:
        out.update(photon_stats(r.p))
        out["fidelity_lossy_fock1"] = fidelity_diag(r.p, lossy_fock1(ef["eta"], nmax))
        out["fidelity_fock1"] = float(r.p[1]) if len(r.p) > 1 else 0.0
        out["wigner_r"] = R_GRID.tolist()
        out["wigner_radial"] = wigner_radial(r.p, R_GRID).tolist()
    out["_p"] = r.p
    return out
