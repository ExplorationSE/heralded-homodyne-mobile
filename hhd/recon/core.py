"""Phasengemittelte Rekonstruktion: Diagonale ρ_nn in Fock-Basis aus Quadraturen x (Vakuumvarianz 1/2).

Für einen phasengemittelten Zustand ist die Randverteilung p(x) = Σ_n ρ_nn ψ_n(x)², unabhängig von θ.

Schätzer
--------
* ``em``       Erwartungsmaximierung ρ_n ← ρ_n · R_n, R_n = ⟨ψ_n²(x)/p(x)⟩ (konvergiert monoton zur MLE);
* ``maxlik``   iterativ RρR (Lvovsky 2004): ρ ← RρR/Tr; für Diagonalzustände ρ_n ← ρ_n R_n²/Σ;
               beide konvergieren zur selben MLE, RρR schneller;
* ``binned``   EM auf einem Histogramm (Bin-Mittelwerte der Muster);
* ``moments``  linearer Momentenschätzer (ohne Positivität, erwartungstreu, wenn ρ_nn = 0 für n > N):
               ⟨x^{2k}⟩ = Σ_n M_kn ρ_nn, k = 0…N.
Optionale Verlustkorrektur: Muster P_n^η(x) = Σ_m B(m; n, η) ψ_m²(x) (Rekonstruktion vor den Verlusten η).

Konvergenz: Abbruch, wenn die Änderung der Log-Likelihood je Iteration < tol·N ist und max |Δρ| < tol.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
from scipy.special import eval_genlaguerre, gammaln

from ..sim.states import hermite_function

ESTIMATORS = ("maxlik", "em", "binned", "moments")


def patterns(x: np.ndarray, nmax: int, eta: float = 1.0) -> np.ndarray:
    """P[n, i] = Verteilung des Fock-Zustands |n⟩ (nach Verlust η) an x_i."""
    x = np.asarray(x, float)
    P = np.empty((nmax + 1, len(x)))
    p0 = np.pi ** -0.25 * np.exp(-x ** 2 / 2)
    P[0] = p0 ** 2
    if nmax >= 1:
        p1 = np.sqrt(2.0) * x * p0
        P[1] = p1 ** 2
        for k in range(2, nmax + 1):
            p0, p1 = p1, np.sqrt(2.0 / k) * x * p1 - np.sqrt((k - 1) / k) * p0
            P[k] = p1 ** 2
    if eta < 1.0:
        P = loss_matrix(nmax, eta).T @ P
    return P


def loss_matrix(nmax: int, eta: float) -> np.ndarray:
    """B[m, n] = P(m Photonen nach Verlust | n vorher) = C(n,m) η^m (1−η)^(n−m)."""
    B = np.zeros((nmax + 1, nmax + 1))
    for n in range(nmax + 1):
        m = np.arange(n + 1)
        lc = gammaln(n + 1) - gammaln(m + 1) - gammaln(n - m + 1)
        with np.errstate(divide="ignore"):
            B[m, n] = np.exp(lc + m * np.log(eta) + (n - m) * np.log1p(-eta)) if 0 < eta < 1 else (m == n * (eta >= 1))
    return B


@dataclass
class ReconResult:
    p: np.ndarray                    # ρ_nn
    estimator: str
    nmax: int
    n_iter: int = 0
    converged: bool = True
    loglik: float = float("nan")
    eta_corrected: float = 1.0
    info: dict = field(default_factory=dict)

    @property
    def W00(self) -> float:
        return wigner00(self.p)

    @property
    def mean_n(self) -> float:
        return float(np.sum(np.arange(len(self.p)) * self.p))

    def to_dict(self) -> dict:
        return dict(estimator=self.estimator, nmax=self.nmax, p_n=self.p.tolist(), W00=self.W00, mean_n=self.mean_n,
                    n_iter=self.n_iter, converged=self.converged, loglik=self.loglik,
                    eta_corrected=self.eta_corrected, **self.info)


def _iterate(P: np.ndarray, w: np.ndarray | None, power: int, max_iter: int, tol: float, p0=None):
    n = P.shape[0]
    p = np.full(n, 1.0 / n) if p0 is None else np.asarray(p0, float).copy()
    W = np.ones(P.shape[1]) if w is None else w
    Wn = W / W.sum()
    ll_old = -np.inf
    for it in range(1, max_iter + 1):
        pr = np.maximum(p @ P, 1e-300)
        R = (P / pr) @ Wn
        q = p * R ** power
        q /= q.sum()
        ll = float(Wn @ np.log(pr))
        dp = float(np.max(np.abs(q - p)))
        p = q
        if dp < tol and abs(ll - ll_old) < tol * 1e-2:
            return p, it, True, ll
        ll_old = ll
    return p, max_iter, False, ll


def reconstruct(x: np.ndarray, nmax: int = 10, estimator: str = "maxlik", eta: float = 1.0, max_iter: int = 20000,
                tol: float = 1e-8, bins: int = 200, p0=None) -> ReconResult:
    x = np.asarray(x, float)
    if estimator in ("maxlik", "em"):
        P = patterns(x, nmax, eta)
        p, it, ok, ll = _iterate(P, None, 2 if estimator == "maxlik" else 1, max_iter, tol, p0)
        return ReconResult(p, estimator, nmax, it, ok, ll * len(x), eta)
    if estimator == "binned":
        lim = max(6.0, float(np.max(np.abs(x))) + 1e-9)
        edges = np.linspace(-lim, lim, bins + 1)
        h, _ = np.histogram(x, edges)
        # Bin-Mittel der Muster (Simpson über 5 Stützstellen je Bin)
        sub = np.linspace(0, 1, 5); wts = np.array([1, 4, 2, 4, 1]) / 12.0
        xs = edges[:-1, None] + (edges[1] - edges[0]) * sub[None, :]
        Pb = (patterns(xs.ravel(), nmax, eta).reshape(nmax + 1, bins, 5) * wts).sum(-1)
        m = h > 0
        p, it, ok, ll = _iterate(Pb[:, m], h[m].astype(float), 2, max_iter, tol, p0)
        return ReconResult(p, estimator, nmax, it, ok, ll * len(x), eta, info=dict(bins=bins))
    if estimator == "moments":
        return moments_estimate(x, nmax=min(nmax, 3), eta=eta)
    raise ValueError(f"Schätzer {estimator!r}: {', '.join(ESTIMATORS)}")


@lru_cache(maxsize=32)
def _moment_matrix(N: int, eta: float) -> np.ndarray:
    xg = np.linspace(-14, 14, 40001); dx = xg[1] - xg[0]
    P = patterns(xg, N, eta)
    return np.array([[np.sum(xg ** (2 * k) * P[n]) * dx for n in range(N + 1)] for k in range(N + 1)])


def moments_estimate(x: np.ndarray, nmax: int = 2, eta: float = 1.0) -> ReconResult:
    """Linearer Momentenschätzer (erwartungstreu für Zustände mit ρ_nn = 0 oberhalb ``nmax``)."""
    M = _moment_matrix(int(nmax), float(eta))
    m = np.array([np.mean(np.asarray(x) ** (2 * k)) for k in range(nmax + 1)])
    p = np.linalg.solve(M, m)
    return ReconResult(p, "moments", nmax, 0, True, float("nan"), eta)


# ------------------------------------------------------------------------------------------ Kenngrößen
def wigner00(p: np.ndarray) -> float:
    p = np.asarray(p, float)
    return float(np.sum(p * (-1.0) ** np.arange(len(p))) / np.pi)


def wigner_radial(p: np.ndarray, r: np.ndarray) -> np.ndarray:
    """W(r) phasengemittelt: Σ ρ_nn (−1)^n/π e^(−r²) L_n(2r²), r² = x² + p²."""
    r = np.asarray(r, float)
    out = np.zeros_like(r)
    for n, pn in enumerate(np.asarray(p, float)):
        out += pn * (-1) ** n / np.pi * np.exp(-r ** 2) * eval_genlaguerre(n, 0, 2 * r ** 2)
    return out


def wigner_grid(p: np.ndarray, lim: float = 4.0, n: int = 81) -> tuple[np.ndarray, np.ndarray]:
    g = np.linspace(-lim, lim, n)
    X, Y = np.meshgrid(g, g)
    return g, wigner_radial(p, np.hypot(X, Y))


def fidelity_diag(p: np.ndarray, q: np.ndarray) -> float:
    """Uhlmann-Fidelity für (phasengemittelt) diagonale Zustände: (Σ √(p_n q_n))²."""
    n = min(len(p), len(q))
    a = np.clip(np.asarray(p[:n], float), 0, None); b = np.clip(np.asarray(q[:n], float), 0, None)
    return float(np.sum(np.sqrt(a * b)) ** 2)


def lossy_fock1(eta: float, nmax: int) -> np.ndarray:
    q = np.zeros(nmax + 1); q[0] = 1 - eta; q[1] = eta
    return q


def fit_eta_fock1(x: np.ndarray, max_iter: int = 200) -> dict:
    """MLE von η im Modell (1−η)|0⟩⟨0| + η|1⟩⟨1| (Einzelphoton nach Verlust; ξ steckt in η)."""
    x = np.asarray(x, float)
    P = patterns(x, 1)
    e = float(np.clip(np.mean(x ** 2) - 0.5, 1e-4, 1 - 1e-4))
    for _ in range(max_iter):
        pr = (1 - e) * P[0] + e * P[1]
        d = P[1] - P[0]
        g = np.sum(d / pr); h = -np.sum((d / pr) ** 2)
        step = -g / h
        e_new = float(np.clip(e + step, 1e-6, 1 - 1e-6))
        if abs(e_new - e) < 1e-10:
            e = e_new; break
        e = e_new
    pr = (1 - e) * P[0] + e * P[1]
    fisher = float(np.sum(((P[1] - P[0]) / pr) ** 2))
    return dict(eta=e, sigma_fisher=float(1 / np.sqrt(fisher)) if fisher > 0 else float("nan"))


def photon_stats(p: np.ndarray) -> dict:
    p = np.asarray(p, float)
    n = np.arange(len(p))
    mn = float(np.sum(n * p)); m2 = float(np.sum(n * (n - 1) * p))
    return dict(mean_n=mn, g2=float(m2 / mn ** 2) if mn > 0 else float("nan"),
                mandel_Q=float((m2 - mn ** 2) / mn) if mn > 0 else float("nan"))
