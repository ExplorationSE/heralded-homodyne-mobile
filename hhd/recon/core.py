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

import os

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


# Beschleunigung der RρR-Iteration (ab 0.15): SQUAREM (Varadhan & Roland 2008, Schema S3) um denselben Fixpunkt-
# Schritt. Ein Zyklus = zwei normale Schritte, Extrapolation, ein Stabilisierungsschritt; das Abbruchkriterium ist das
# der normalen Iteration (am Stabilisierungsschritt). Sinkt die Likelihood gegenüber dem letzten akzeptierten Punkt
# oder verlässt die Extrapolation den Simplex, gilt der normale Doppelschritt (Rückfall); nach 5 Rückfällen in Folge
# folgen 50 normale Schritte. Abbruch zusätzlich erst bei erfüllter KKT-Bedingung (siehe unten).
# Messung (squeezed 6 dB und Fock 2, je 16 Bootstrap-Ziehungen warm/kalt, tol 1e-8): Abstand zur bis tol 1e-14
# auskonvergierten Lösung |ΔW(0,0)| ≤ 2,6e-6 (normale Iteration bei tol 1e-8: bis 2,4e-3 durch Scheinkonvergenz).
# ``HHD_MAXLIK_ACCEL=0`` schaltet ab (Vergleich, Fehlersuche). ``n_iter`` = Zahl der Schritte.
ACCEL = os.environ.get("HHD_MAXLIK_ACCEL", "1").strip() not in ("0", "false", "no", "aus")
KKT_TOL = 1e-4
# Histogramm-MaxLik (estimator "binned", ab binned_above Werten je Gruppe): Binbreite. Bis 0.14: 200 Bins über
# ±max(6, max|x|) (≥ 0,06). Gemessen (squeezed 6 dB, 12 Bootstrap-Ziehungen, gegen die auskonvergierte MaxLik ohne
# Bins): Binfehler |ΔW(0,0)| bis 3,0e-3 bei 200 Bins, ≤ 4,8e-4 bei 0,012, ≤ 3,1e-4 bei 0,006. Die > 1σ-Abweichung
# (bis 1,5e-2) kam von der Scheinkonvergenz der Iteration (Warmstart, tol 1e-7), behoben durch das KKT-Kriterium.
BINNED_WIDTH = 0.01


def _step(P, Wn, p, power):
    """Ein RρR-Schritt (diagonal): q ∝ p·R^power mit R = P·(w/pr); Rückgabe (q, mittlere Log-Likelihood bei p, R)."""
    pr = np.maximum(p @ P, 1e-300)
    R = P @ (Wn / pr)
    q = p * (R * R if power == 2 else R ** power)
    q /= q.sum()
    return q, float(Wn @ np.log(pr)), R


def _loglik(P, Wn, p) -> float:
    return float(Wn @ np.log(np.maximum(p @ P, 1e-300)))


def _iterate(P: np.ndarray, w: np.ndarray | None, power: int, max_iter: int, tol: float, p0=None, accel=None):
    n = P.shape[0]
    p = np.full(n, 1.0 / n) if p0 is None else np.asarray(p0, float).copy()
    W = np.ones(P.shape[1]) if w is None else w
    Wn = W / W.sum()
    accel = ACCEL if accel is None else bool(accel)
    if accel and p0 is not None:
        # Warmstart (Bootstrap: Punktschätzung) mit 0,1 % Gleichverteilung mischen: Komponenten nahe 0 (bis 1e-70
        # gesehen) kann die multiplikative Iteration sonst kaum anheben → Scheinkonvergenz am falschen Punkt.
        p = (1.0 - 1e-3) * p / p.sum() + 1e-3 / n
    ll_old = -np.inf
    it = 0
    fails = 0
    plain_left = 0
    while it < max_iter:
        if accel and fails >= 5:                                   # nach 5 Rückfällen in Folge 50 normale Schritte
            fails, plain_left = 0, 50
        if accel and plain_left == 0 and it + 3 <= max_iter:
            p1, ll0, _ = _step(P, Wn, p, power)
            p2, _, _ = _step(P, Wn, p1, power)
            it += 2
            r = p1 - p
            v = p2 - p1 - r
            nv = float(np.linalg.norm(v))
            a = min(-float(np.linalg.norm(r)) / nv, -1.0) if nv > 0 else -1.0
            pn = p - 2.0 * a * r + a * a * v
            if a < -1.0 and (np.any(pn < 0) or not np.all(np.isfinite(pn))):
                pn = None                                          # außerhalb des Simplex → normaler Doppelschritt
                fails += 1
            if pn is not None:
                pn = pn / pn.sum()
            q, ll, R = _step(P, Wn, pn if pn is not None else p2, power)
            it += 1
            if pn is not None and a < -1.0 and ll < ll0:          # Likelihood gesunken → normaler Doppelschritt
                fails += 1
                q, ll, R = _step(P, Wn, p2, power)
                it += 1
                pn = p2
            else:
                if pn is not None and a < -1.0:
                    fails = 0
                pn = p2 if pn is None else pn
            dp = float(np.max(np.abs(q - pn)))
        else:
            pn = p
            q, ll, R = _step(P, Wn, p, power)
            it += 1
            plain_left = max(plain_left - 1, 0)
            dp = float(np.max(np.abs(q - p)))
            kkt_ok = True
        if accel:
            # Zusätzlich KKT (Optimalität): R ≤ 1 + KKT_TOL auf dem Träger. Die multiplikative Iteration kann bei sehr
            # kleinen Komponenten scheinbar stehen (dp, Δll klein, aber noch nicht am Maximum; gemessen ΔW bis 1,5e-2
            # bei Warmstart und tol 1e-7). Exakte Nullen bleiben außen vor, sie kann die Iteration nicht anheben.
            sup = pn > 0.0
            kkt_ok = not np.any(sup) or float(np.max(R[sup])) - 1.0 < KKT_TOL
        p = q
        if dp < tol and abs(ll - ll_old) < tol * 1e-2 and kkt_ok:
            return p, it, True, ll
        ll_old = ll
    return p, max_iter, False, ll


def reconstruct(x: np.ndarray, nmax: int = 10, estimator: str = "maxlik", eta: float = 1.0, max_iter: int = 20000,
                tol: float = 1e-8, bins: int | None = None, p0=None) -> ReconResult:
    x = np.asarray(x, float)
    if estimator in ("maxlik", "em"):
        P = patterns(x, nmax, eta)
        p, it, ok, ll = _iterate(P, None, 2 if estimator == "maxlik" else 1, max_iter, tol, p0)
        return ReconResult(p, estimator, nmax, it, ok, ll * len(x), eta)
    if estimator == "binned":
        lim = max(6.0, float(np.max(np.abs(x))) + 1e-9)
        if not bins:                                   # ab 0.15: feste Breite statt 200 Bins (siehe BINNED_WIDTH)
            bins = int(np.ceil(2 * lim / BINNED_WIDTH))
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


G2_MIN_SIGNIFICANCE = 2.0     # g²(0) nur, wenn ⟨n⟩ mindestens so viele Standardfehler über 0 liegt


def photon_stat_errors(point: dict, boot: dict, k: float = G2_MIN_SIGNIFICANCE) -> dict:
    """Bootstrap-Fehler für Mandel Q und g²(0) aus den Ziehungen (Werte je Ziehung aus p_n).

    g²(0) = ⟨n(n−1)⟩/⟨n⟩² ist bei ⟨n⟩ → 0 nicht definiert: Ziehungen mit ⟨n⟩ ≈ 1e-6 ergeben Werte bis 1e10 und eine
    sinnlose Standardabweichung (z. B. Lauf B ≈ Vakuum: ⟨n⟩ = 0,005 ± 0,010 → „g² = 0,01 ± 6·10⁹“). Ist ⟨n⟩ nicht
    signifikant größer als 0 (⟨n⟩ ≤ k·σ(⟨n⟩), σ aus dem Bootstrap), werden g²(0) und sein Fehler als nicht definiert
    (nan → ``null``) ausgegeben, mit Begründung in ``g2_note``.
    """
    out = {}
    for q in ("mandel_Q", "g2"):
        v = np.asarray(boot.get(q, []), float); v = v[np.isfinite(v)]
        out[f"{q}_err"] = float(np.std(v, ddof=1)) if len(v) > 2 else float("nan")
        out[f"{q}_n_boot"] = int(len(v))
    mn = float(point.get("mean_n", float("nan")))
    mb = np.asarray(boot.get("mean_n", []), float); mb = mb[np.isfinite(mb)]
    se = float(np.std(mb, ddof=1)) if len(mb) > 2 else float("nan")
    if not (np.isfinite(mn) and mn > 0 and np.isfinite(se) and mn > k * se):
        out.update(g2=float("nan"), g2_err=float("nan"),
                   g2_note=f"nicht definiert: ⟨n⟩ = {mn:.3g} ± {se:.2g} nicht signifikant > 0")
    return out


def photon_stats(p: np.ndarray) -> dict:
    p = np.asarray(p, float)
    n = np.arange(len(p))
    mn = float(np.sum(n * p)); m2 = float(np.sum(n * (n - 1) * p))
    return dict(mean_n=mn, g2=float(m2 / mn ** 2) if mn > 0 else float("nan"),
                mandel_Q=float((m2 - mn ** 2) / mn) if mn > 0 else float("nan"))
