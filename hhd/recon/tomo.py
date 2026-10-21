"""Quantenzustandstomografie mit bekannter Phase θ (ab 0.15, ED-11): MaxLik (RρR) in Fock-Basis.

Eingang ist ein Histogramm der normierten Quadraturen über θ × x (``PhaseHist``, speicherbegrenzt, auch im
Datenstrom). POVM eines Bins (θ_j, x_k) mit Detektionseffizienz η:

    E_jk = L_η†(Π_jk),  Π_jk = ⟨|x_θ⟩⟨x_θ|⟩_{θ ∈ Bin j, x ∈ Bin k},  ⟨m|Π|n⟩ = ψ_m(x)ψ_n(x)·e^{i(m−n)θ}

(Konvention x_θ = (a e^{−iθ} + a† e^{iθ})/√2, Vakuumvarianz ½; L_η Verlustkanal, L_η† adjungiert). Die Phase
faktorisiert: E_jk = Φ_j ∘ J_k mit Φ_j,mn = ⟨e^{i(m−n)θ}⟩_j und J_k = L_η†(∫_k ψ_m ψ_n dx) – dadurch braucht die
Iteration ρ ← RρR/Tr nur O(n_θ·n_x·N²) je Schritt. Abschneidegrenze N automatisch aus ⟨n⟩ der Daten.

Kennwerte aus ρ: W(x, p) (2D, iterativ nach Laguerre), W(0,0) = Parität/π, ⟨n⟩, Reinheit, Var(x_θ) mit
Quetschung (dB) und Achse, α einer Katze aus ⟨a²⟩ (für Katzen exakt α²·η), Treue zu einem Sollzustand
(maximiert über eine Drehung e^{−inφ}, da die Referenzphase des Zustands nicht bekannt sein muss).
"""
from __future__ import annotations

import numpy as np
from scipy.linalg import sqrtm
from scipy.special import gammaln

VAC = 0.5


def hermite_table(N: int, x: np.ndarray) -> np.ndarray:
    """ψ_n(x) für n < N (N × len(x)), stabile Rekursion."""
    x = np.asarray(x, float)
    out = np.empty((N, len(x)))
    out[0] = np.pi ** -0.25 * np.exp(-x ** 2 / 2)
    if N > 1:
        out[1] = np.sqrt(2.0) * x * out[0]
    for k in range(2, N):
        out[k] = np.sqrt(2.0 / k) * x * out[k - 1] - np.sqrt((k - 1) / k) * out[k - 2]
    return out


# ---------------------------------------------------------------- Histogramm θ × x
class PhaseHist:
    """Feines Histogramm (θ: ``n_theta`` Bins über [0, 2π), x: Breite ``dx`` über ±``xmax``) plus Summen je θ-Bin."""

    def __init__(self, n_theta: int = 96, dx: float = 0.05, xmax: float = 8.0):
        self.n_theta, self.dx, self.xmax = n_theta, dx, xmax
        self.nx = int(round(2 * xmax / dx))
        self.counts = np.zeros((n_theta, self.nx), np.int64)
        self.s = np.zeros((n_theta, 3))             # n, Σx, Σx² je θ-Bin (exakt, ohne Binning in x)
        self.n_outside = 0

    def add(self, x: np.ndarray, theta: np.ndarray) -> None:
        x = np.asarray(x, float); th = np.mod(np.asarray(theta, float), 2 * np.pi)
        m = np.isfinite(x) & np.isfinite(th)
        x, th = x[m], th[m]
        j = np.minimum((th / (2 * np.pi) * self.n_theta).astype(np.int64), self.n_theta - 1)
        k = np.floor((x + self.xmax) / self.dx).astype(np.int64)
        ok = (k >= 0) & (k < self.nx)
        self.n_outside += int((~ok).sum())
        np.add.at(self.counts, (j[ok], k[ok]), 1)
        np.add.at(self.s, j, np.stack([np.ones(len(x)), x, x * x], 1))

    def merge(self, o: "PhaseHist") -> "PhaseHist":
        self.counts += o.counts; self.s += o.s; self.n_outside += o.n_outside
        return self

    @property
    def n(self) -> int:
        return int(self.s[:, 0].sum())

    def theta_edges(self, f: int = 1) -> np.ndarray:
        return np.linspace(0, 2 * np.pi, self.n_theta // f + 1)

    def coarse(self, f_theta: int = 3, f_x: int = 3, xmax: float | None = None):
        """(counts (n_θ', n_x'), θ-Kanten, x-Kanten) vergröbert; x auf ±xmax beschnitten (Rand → äußerster Bin)."""
        c = self.counts.reshape(self.n_theta // f_theta, f_theta, self.nx).sum(1)
        nxc = self.nx // f_x
        c = c[:, :nxc * f_x].reshape(c.shape[0], nxc, f_x).sum(2)
        xe = -self.xmax + np.arange(nxc + 1) * self.dx * f_x
        if xmax is not None and xmax < self.xmax:
            lo = int(np.searchsorted(xe, -xmax - 1e-9)); hi = int(np.searchsorted(xe, xmax + 1e-9))
            hi = max(hi, lo + 2)
            cc = c[:, lo:hi - 1].copy()
            cc[:, 0] += c[:, :lo].sum(1); cc[:, -1] += c[:, hi - 1:].sum(1)
            c, xe = cc, xe[lo:hi]
        return c, self.theta_edges(f_theta), xe

    def var_theta(self, f_theta: int = 4) -> dict:
        """⟨x⟩, Var x je (grobem) θ-Bin mit Fehlern (aus den exakten Summen)."""
        s = self.s.reshape(self.n_theta // f_theta, f_theta, 3).sum(1)
        n = s[:, 0]
        with np.errstate(invalid="ignore", divide="ignore"):
            mu = s[:, 1] / n
            var = s[:, 2] / n - mu ** 2
            var = var * n / np.maximum(n - 1, 1)
            err = var * np.sqrt(2.0 / np.maximum(n - 1, 1))
            mu_err = np.sqrt(var / n)
        e = self.theta_edges(f_theta)
        return dict(theta=0.5 * (e[1:] + e[:-1]), n=n, mean=mu, mean_err=mu_err, var=var, var_err=err)


# ---------------------------------------------------------------- POVM
def loss_adjoint(O: np.ndarray, eta: float) -> np.ndarray:
    """L_η†(O) in Fock-Basis: (L†O)_mn = Σ_l √(B(m,l)B(n,l))·O_{m−l, n−l}, B(n,l) = C(n,l) η^{n−l} (1−η)^l."""
    if eta >= 1:
        return O.copy()
    N = O.shape[-1]
    n = np.arange(N)
    out = np.zeros_like(O)
    for l in range(N):
        m = n[l:]
        lb = gammaln(m + 1) - gammaln(l + 1) - gammaln(m - l + 1) + (m - l) * np.log(eta) + l * np.log1p(-eta)
        b = np.exp(0.5 * lb)
        out[..., l:, l:] += b[:, None] * b[None, :] * O[..., :N - l, :N - l]
    return out


def bin_povm_x(N: int, x_edges: np.ndarray, eta: float, nq: int = 6) -> np.ndarray:
    """J_k = L_η†(∫_{Bin k} ψ_m ψ_n dx), reell symmetrisch, Form (n_x, N, N). Äußere Bins reichen bis ±∞."""
    gx, gw = np.polynomial.legendre.leggauss(nq)
    xe = np.asarray(x_edges, float).copy()
    nx = len(xe) - 1
    I = np.zeros((nx, N, N))
    for k in range(nx):
        a, b = xe[k], xe[k + 1]
        if k == 0:
            a = a - 6.0
        if k == nx - 1:
            b = b + 6.0
        xs = 0.5 * (b - a) * gx + 0.5 * (b + a)
        ws = 0.5 * (b - a) * gw
        if k == 0 or k == nx - 1:                    # breite Randbins feiner integrieren
            sub = np.linspace(a, b, 9)
            xs = np.concatenate([0.5 * (sub[i + 1] - sub[i]) * gx + 0.5 * (sub[i + 1] + sub[i]) for i in range(8)])
            ws = np.concatenate([0.5 * (sub[i + 1] - sub[i]) * gw for i in range(8)])
        H = hermite_table(N, xs)
        I[k] = (H * ws) @ H.T
    return loss_adjoint(I, eta)


def phase_factors(N: int, theta_edges: np.ndarray, nsub: int = 8) -> np.ndarray:
    """Φ_j,mn = ⟨e^{i(m−n)θ}⟩ über Bin j (gleichverteilt im Bin), Form (n_θ, N, N)."""
    e = np.asarray(theta_edges, float)
    nt = len(e) - 1
    d = np.arange(N)[:, None] - np.arange(N)[None, :]
    out = np.zeros((nt, N, N), complex)
    for j in range(nt):
        th = e[j] + (np.arange(nsub) + 0.5) / nsub * (e[j + 1] - e[j])
        out[j] = np.exp(1j * d[None] * th[:, None, None]).mean(0)
    return out


# ---------------------------------------------------------------- MaxLik
class Tomography:
    """MaxLik-Rekonstruktion aus einem θ×x-Histogramm (Zähler ``counts`` (n_θ, n_x))."""

    def __init__(self, counts: np.ndarray, theta_edges: np.ndarray, x_edges: np.ndarray, eta: float, N: int):
        self.counts = np.asarray(counts, float)
        self.N, self.eta = int(N), float(eta)
        self.Phi = phase_factors(N, theta_edges).reshape(len(theta_edges) - 1, N * N)
        self.J = bin_povm_x(N, x_edges, eta).reshape(len(x_edges) - 1, N * N)
        self.theta_edges, self.x_edges = theta_edges, x_edges

    def probs(self, rho: np.ndarray) -> np.ndarray:
        A = self.Phi * rho.T.reshape(1, -1)
        return np.real(A @ self.J.T)

    def loglik(self, rho: np.ndarray, counts=None) -> float:
        c = self.counts if counts is None else counts
        p = np.clip(self.probs(rho), 1e-300, None)
        # bedingte Likelihood je θ-Bin (Anteil der Records je θ ist vorgegeben)
        pn = p / np.maximum(p.sum(1, keepdims=True), 1e-300)
        return float(np.sum(c * np.log(pn)))

    def fit(self, rho0: np.ndarray | None = None, counts=None, max_iter: int = 3000, tol: float = 1e-10) -> dict:
        c = self.counts if counts is None else np.asarray(counts, float)
        N = self.N
        rho = np.eye(N, dtype=complex) / N if rho0 is None else rho0.astype(complex)
        tot = c.sum()
        ll_old = -np.inf
        it = 0
        for it in range(1, max_iter + 1):
            p = np.clip(self.probs(rho), 1e-300, None)
            w = c / p / tot
            S = w @ self.J                               # (n_θ, N²)
            R = (self.Phi * S).sum(0).reshape(N, N)
            R = 0.5 * (R + R.conj().T)
            rho = R @ rho @ R
            rho = 0.5 * (rho + rho.conj().T)
            rho /= np.real(np.trace(rho))
            if it % 20 == 0 or it == max_iter:
                ll = float(np.sum(c * np.log(np.clip(self.probs(rho), 1e-300, None))))
                if abs(ll - ll_old) < tol * max(1.0, abs(ll)):
                    break
                ll_old = ll
        return dict(rho=rho, n_iter=it, loglik=self.loglik(rho, c))


# ---------------------------------------------------------------- Kennwerte
def wigner2d(rho: np.ndarray, xvec: np.ndarray, pvec: np.ndarray) -> np.ndarray:
    """W(x, p) aus ρ (Konvention x = (a + a†)/√2, W_Vakuum(0,0) = 1/π), Laguerre-Iteration. Form (len(p), len(x))."""
    M = rho.shape[0]
    X, Y = np.meshgrid(xvec, pvec)
    A = (X + 1j * Y) / np.sqrt(2)                    # 0.5·g·(x + iy) mit g = √2
    Wl = [np.exp(-2.0 * np.abs(A) ** 2) / np.pi]
    W = np.real(rho[0, 0]) * np.real(Wl[0])
    for n in range(1, M):
        Wl.append(2.0 * A * Wl[n - 1] / np.sqrt(n))
        W = W + 2 * np.real(rho[0, n] * Wl[n])
    for m in range(1, M):
        temp = Wl[m].copy()
        Wl[m] = (2 * np.conj(A) * temp - np.sqrt(m) * Wl[m - 1]) / np.sqrt(m)
        W = W + np.real(rho[m, m] * Wl[m])
        for n in range(m + 1, M):
            temp2 = (2 * A * Wl[n - 1] - np.sqrt(m) * temp) / np.sqrt(n)
            temp = Wl[n].copy()
            Wl[n] = temp2
            W = W + 2 * np.real(rho[m, n] * Wl[n])
    return W                                          # 0.5·g² = 1


def moments(rho: np.ndarray) -> tuple[complex, complex, float]:
    N = rho.shape[0]
    a = np.diag(np.sqrt(np.arange(1, N)), 1)
    return complex(np.trace(rho @ a)), complex(np.trace(rho @ a @ a)), float(np.real(np.trace(rho @ a.T @ a)))


def var_curve(rho: np.ndarray, theta: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    ma, ma2, n = moments(rho)
    th = np.asarray(theta, float)
    mu = np.sqrt(2) * np.real(ma * np.exp(-1j * th))
    return mu, np.real(ma2 * np.exp(-2j * th)) + n + VAC - mu ** 2


def squeezing(rho: np.ndarray) -> dict:
    th = np.linspace(0, np.pi, 721)
    _, v = var_curve(rho, th)
    i, j = int(np.argmin(v)), int(np.argmax(v))
    return dict(var_min=float(v[i]), var_max=float(v[j]), axis_rad=float(th[i]),
                squeezing_dB=float(10 * np.log10(v[i] / VAC)), antisqueezing_dB=float(10 * np.log10(v[j] / VAC)))


def rotate(rho: np.ndarray, phi: float) -> np.ndarray:
    """U ρ U† mit U = e^{−i n φ} (Drehung im Phasenraum um −φ … Konvention wie x_θ)."""
    u = np.exp(-1j * np.arange(rho.shape[0]) * phi)
    return u[:, None] * rho * np.conj(u)[None, :]


def fidelity(rho: np.ndarray, sigma: np.ndarray) -> float:
    """Uhlmann-Treue F = (Tr √(√ρ σ √ρ))²."""
    N = min(rho.shape[0], sigma.shape[0])
    r, s = rho[:N, :N], sigma[:N, :N]
    sr = sqrtm(r)
    v = np.real(np.trace(sqrtm(sr @ s @ sr)))
    return float(np.clip(v ** 2, 0, 1))


def fidelity_max_rotation(rho: np.ndarray, sigma: np.ndarray, pure: bool = False) -> tuple[float, float]:
    """max_φ F(ρ, U(φ)σU(φ)†) und φ (Gitter 2°, dann fein)."""
    N = min(rho.shape[0], sigma.shape[0])
    r, s = rho[:N, :N], sigma[:N, :N]
    if pure:
        w, V = np.linalg.eigh(s)
        psi = V[:, -1]
        f = lambda phi: float(np.real(np.conj(rotate_vec(psi, phi)) @ r @ rotate_vec(psi, phi)))
    else:
        f = lambda phi: fidelity(r, rotate(s, phi))
    grid = np.linspace(0, 2 * np.pi, 181 if pure else 61, endpoint=False)
    vals = [f(g) for g in grid]
    k = int(np.argmax(vals)); best = (vals[k], grid[k])
    step = grid[1] - grid[0]
    for _ in range(3):
        fine = best[1] + np.linspace(-step, step, 11)
        vv = [f(g) for g in fine]
        kk = int(np.argmax(vv)); best = (vv[kk], fine[kk]); step /= 5
    return float(best[0]), float(np.mod(best[1], 2 * np.pi))


def rotate_vec(psi: np.ndarray, phi: float) -> np.ndarray:
    return np.exp(-1j * np.arange(len(psi)) * phi) * psi


def state_metrics(rho: np.ndarray) -> dict:
    N = rho.shape[0]
    p = np.real(np.diag(rho))
    ma, ma2, n = moments(rho)
    par = float(np.sum(p * (-1.0) ** np.arange(N)))
    sq = squeezing(rho)
    nn = np.arange(N)
    f2 = float(np.sum(nn * (nn - 1) * p))
    mq = float((f2 - n ** 2) / n) if n > 1e-12 else float("nan")       # Mandel Q aus ρ (phasenaufgelöst)
    return dict(N=N, mean_n=n, parity=par, mandel_Q=mq, W00=par / np.pi, purity=float(np.real(np.trace(rho @ rho))),
                mean_a=[float(ma.real), float(ma.imag)], abs_mean_a=float(abs(ma)),
                a2=[float(ma2.real), float(ma2.imag)], alpha_from_a2=float(np.sqrt(abs(ma2))),
                alpha_axis_rad=float(np.mod(np.angle(ma2) / 2, np.pi)), p_n=p[:min(N, 16)].tolist(),
                tail=float(p[-2:].sum()), **sq)


def auto_cutoff(mean_n_state: float, lo: int = 8, hi: int = 30) -> int:
    n = max(0.0, float(mean_n_state))
    return int(np.clip(np.ceil(n + 5 * np.sqrt(n + 1) + 4), lo, hi))


def fit_var_theta(vt: dict) -> dict:
    """Fit Var(x_θ) = c + a·cos2θ + b·sin2θ (gewichtet) → Quetschung (dB) und Achse am Detektor; ⟨x⟩ = A·cos(θ−φ)."""
    th, v, e, n = vt["theta"], vt["var"], vt["var_err"], vt["n"]
    m = (n > 10) & np.isfinite(v) & (e > 0)
    if m.sum() < 4:
        return dict(available=False)
    X = np.stack([np.ones(m.sum()), np.cos(2 * th[m]), np.sin(2 * th[m])], 1)
    W = 1 / e[m] ** 2
    C = np.linalg.inv(X.T @ (X * W[:, None]))
    c, a, b = C @ (X.T @ (W * v[m]))
    # Bin-Mittelung: Var im θ-Bin der Breite Δ ist der Mittelwert über das Bin, die cos2θ/sin2θ-Amplitude ist um
    # sinc = sin Δ/Δ verkleinert (bei starker Antiquetschung, z. B. Katze α = 2, sonst +0,3 dB Bias in Var_min)
    dth = float(np.median(np.diff(np.sort(th)))) if len(th) > 1 else 0.0
    sinc = float(np.sin(dth) / dth) if dth > 0 else 1.0
    a, b = a / sinc, b / sinc
    C = C.copy(); C[1:, :] /= sinc; C[:, 1:] /= sinc
    R = float(np.hypot(a, b))
    vmin, vmax = c - R, c + R
    # Fehler von vmin
    g = np.array([1.0, -a / R if R > 0 else 0.0, -b / R if R > 0 else 0.0])
    vmin_err = float(np.sqrt(g @ C @ g))
    chi2 = float(np.sum(W * (v[m] - X @ np.array([c, a * sinc, b * sinc])) ** 2))
    axis = float(np.mod(0.5 * np.arctan2(b, a) + np.pi / 2, np.pi))
    mu, me = vt["mean"], vt["mean_err"]
    Xm = np.stack([np.cos(th[m]), np.sin(th[m])], 1); Wm = 1 / np.maximum(me[m], 1e-9) ** 2
    Cm = np.linalg.inv(Xm.T @ (Xm * Wm[:, None])); cm = Cm @ (Xm.T @ (Wm * mu[m]))
    return dict(available=True, c=float(c), a=float(a), b=float(b), var_min=float(vmin), var_min_err=vmin_err,
                var_max=float(vmax), axis_rad=axis, chi2=chi2, ndof=int(m.sum() - 3),
                squeezing_dB=float(10 * np.log10(vmin / VAC)) if vmin > 0 else float("nan"),
                squeezing_dB_err=float(10 / np.log(10) * vmin_err / vmin) if vmin > 0 else float("nan"),
                antisqueezing_dB=float(10 * np.log10(vmax / VAC)),
                mean_amp=float(np.hypot(*cm)), mean_phase_rad=float(np.arctan2(cm[1], cm[0])))


def coverage(vt: dict, rel_min: float = 0.25) -> dict:
    n = np.asarray(vt["n"], float)
    mean = n.mean() if len(n) else 0.0
    gaps = [int(i) for i in np.flatnonzero(n < rel_min * mean)]
    return dict(theta=vt["theta"].tolist(), counts=n.astype(int).tolist(), gaps=gaps,
                min_rel=float(n.min() / mean) if mean > 0 else 0.0, ok=not gaps)
