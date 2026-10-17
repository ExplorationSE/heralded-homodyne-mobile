"""Vorbereitete Schnittstelle: Rekonstruktion mit bekannter Phase θ (Kann, ED-11; nicht Teil von v1.0).

Eingang: Quadraturen x_i und Phasen θ_i (aus dem Phasenreferenz-Marker M2 bzw. einer Phasenmessung).
Vorgesehen: MaxLik RρR mit Projektoren |x,θ⟩⟨x,θ| in Fock-Basis (volle Dichtematrix), Wigner-Funktion in 2D.
Derzeit umgesetzt: Prüfgröße ⟨x⟩(θ) = A·cos(θ − φ) (Phasenfit). ``reconstruct_with_phase`` meldet
NotImplementedError, bis ED-11 entschieden ist.
"""
from __future__ import annotations

import numpy as np


def phase_fit(x: np.ndarray, theta: np.ndarray) -> dict:
    m = np.isfinite(theta)
    X = np.stack([np.cos(theta[m]), np.sin(theta[m])], 1)
    coef, *_ = np.linalg.lstsq(X, x[m], rcond=None)
    res = x[m] - X @ coef
    cov = np.linalg.inv(X.T @ X) * res.var()
    a, b = coef
    A = float(np.hypot(a, b))
    return dict(A=A, phi=float(np.arctan2(b, a)),
                A_err=float(np.sqrt((a * a * cov[0, 0] + b * b * cov[1, 1]) / A ** 2)) if A > 0 else float("nan"),
                n=int(m.sum()))


def reconstruct_with_phase(x: np.ndarray, theta: np.ndarray, nmax: int = 10):
    raise NotImplementedError("Rekonstruktion mit bekannter Phase: Option ED-11, Schnittstelle vorbereitet")
