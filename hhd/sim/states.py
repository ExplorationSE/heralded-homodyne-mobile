"""Zustandsmodelle für die Quadratur einer temporalen Mode.

Konvention: x_θ = (a e^{−iθ} + a† e^{iθ})/√2, Vakuumvarianz 1/2, W(0,0) des Vakuums = 1/π.

Verluste (Effizienz η) werden **exakt** als Strahlteiler mit Vakuum modelliert:
x_mess = √η·x + √(1−η)·v, v ~ N(0, 1/2). Für die Randverteilungen der Homodynmessung ist das
gleichbedeutend mit dem Verlustkanal auf ρ (z. B. η|1⟩⟨1| + (1−η)|0⟩⟨0| für das Einzelphoton).

Sollwerte (Fock-Diagonale, W(0,0), ⟨x⟩, Var x) liefert ``expected(eta)`` aus der Dichtematrix
(abgeschnittene Fock-Basis, Verlustkanal über Binomialkoeffizienten).
"""
from __future__ import annotations

from dataclasses import dataclass
from math import factorial

import numpy as np
from scipy.special import eval_hermite, gammaln

VAC_VAR = 0.5


def hermite_function(n: int, x: np.ndarray) -> np.ndarray:
    """ψ_n(x) = (2^n n! √π)^(−1/2) H_n(x) e^(−x²/2) (stabil über Rekursion)."""
    x = np.asarray(x, dtype=float)
    p0 = np.pi ** -0.25 * np.exp(-x ** 2 / 2)
    if n == 0:
        return p0
    p1 = np.sqrt(2.0) * x * p0
    for k in range(2, n + 1):
        p0, p1 = p1, np.sqrt(2.0 / k) * x * p1 - np.sqrt((k - 1) / k) * p0
    return p1


def loss_channel(rho: np.ndarray, eta: float) -> np.ndarray:
    """Verlustkanal auf eine Dichtematrix in Fock-Basis (exakt bis zur Abschneidegrenze)."""
    N = rho.shape[0]
    out = np.zeros_like(rho, dtype=complex)
    n = np.arange(N)
    for k in range(N):                      # k verlorene Photonen
        # Kraus E_k = Σ_n sqrt(C(n,k)) η^((n−k)/2) (1−η)^(k/2) |n−k⟩⟨n|
        E = np.zeros((N, N))
        for m in range(k, N):
            E[m - k, m] = np.exp(0.5 * (gammaln(m + 1) - gammaln(k + 1) - gammaln(m - k + 1))) * \
                eta ** ((m - k) / 2) * (1 - eta) ** (k / 2)
        out += E @ rho @ E.T
    return out


def _x_operator(N: int, theta: float) -> np.ndarray:
    a = np.diag(np.sqrt(np.arange(1, N)), 1)
    X = (a * np.exp(-1j * theta) + a.T * np.exp(1j * theta)) / np.sqrt(2)
    return X


class State:
    """Basisklasse. ``sample(rng, theta)`` liefert verlustfreie Quadraturen für die Phasen ``theta``."""
    name = "state"
    phase_sensitive = False
    n_fock = 40

    def sample(self, rng: np.random.Generator, theta: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def rho(self) -> np.ndarray:
        raise NotImplementedError

    def measure(self, rng: np.random.Generator, theta: np.ndarray, eta: float) -> np.ndarray:
        theta = np.asarray(theta, dtype=float)
        x = self.sample(rng, theta)
        if eta < 1:
            x = np.sqrt(eta) * x + np.sqrt(1 - eta) * rng.normal(0, np.sqrt(VAC_VAR), x.shape)
        return x

    def expected(self, eta: float = 1.0, theta: float = 0.0) -> dict:
        r = loss_channel(self.rho(), eta) if eta < 1 else self.rho().astype(complex)
        p = np.real(np.diag(r))
        X = _x_operator(r.shape[0], theta)
        mean = float(np.real(np.trace(r @ X)))
        var = float(np.real(np.trace(r @ X @ X))) - mean ** 2
        return dict(p_n=p[:12].tolist(), W00=float(np.sum(p * (-1.0) ** np.arange(len(p))) / np.pi),
                    mean_x=mean, var_x=var, mean_n=float(np.sum(np.arange(len(p)) * p)),
                    trunc_error=float(max(0.0, 1 - p.sum())))

    def describe(self) -> dict:
        return dict(type=self.name)


@dataclass
class Vacuum(State):
    name = "vacuum"

    def sample(self, rng, theta):
        return rng.normal(0, np.sqrt(VAC_VAR), np.shape(theta))

    def rho(self):
        r = np.zeros((self.n_fock, self.n_fock)); r[0, 0] = 1
        return r


@dataclass
class Coherent(State):
    alpha: complex = 1.0
    name = "coherent"
    phase_sensitive = True

    def sample(self, rng, theta):
        mu = np.sqrt(2) * np.real(self.alpha * np.exp(-1j * np.asarray(theta)))
        return mu + rng.normal(0, np.sqrt(VAC_VAR), np.shape(theta))

    def rho(self):
        n = np.arange(self.n_fock)
        c = np.exp(-abs(self.alpha) ** 2 / 2 + n * np.log(complex(self.alpha) + 0j) - 0.5 * gammaln(n + 1)) \
            if self.alpha != 0 else (n == 0).astype(complex)
        return np.outer(c, np.conj(c))

    def describe(self):
        return dict(type=self.name, alpha_re=float(np.real(self.alpha)), alpha_im=float(np.imag(self.alpha)))


@dataclass
class Thermal(State):
    nbar: float = 0.1
    name = "thermal"

    def sample(self, rng, theta):
        return rng.normal(0, np.sqrt(VAC_VAR * (2 * self.nbar + 1)), np.shape(theta))

    def rho(self):
        n = np.arange(self.n_fock)
        p = self.nbar ** n / (1 + self.nbar) ** (n + 1)
        return np.diag(p)

    def describe(self):
        return dict(type=self.name, nbar=self.nbar)


class _GridSampler:
    """Inverse-CDF-Abtastung auf einem x-Gitter, optional je Phasen-Stützstelle."""

    def __init__(self, density, n_theta: int, xmax: float = 9.0, nx: int = 4001):
        self.x = np.linspace(-xmax, xmax, nx)
        self.thetas = np.linspace(0, 2 * np.pi, n_theta, endpoint=False) if n_theta > 1 else np.zeros(1)
        cdf = []
        for th in self.thetas:
            p = np.clip(density(self.x, th), 0, None)
            c = np.concatenate([[0], np.cumsum(0.5 * (p[1:] + p[:-1]) * np.diff(self.x))])
            cdf.append(c / c[-1])
        self.cdf = np.array(cdf)

    def __call__(self, rng, theta):
        theta = np.asarray(theta, dtype=float)
        u = rng.random(theta.shape)
        if len(self.thetas) == 1:
            return np.interp(u, self.cdf[0], self.x)
        k = np.rint(np.mod(theta, 2 * np.pi) / (2 * np.pi) * len(self.thetas)).astype(int) % len(self.thetas)
        out = np.empty(theta.shape)
        for j in np.unique(k):
            m = k == j
            out[m] = np.interp(u[m], self.cdf[j], self.x)
        return out


@dataclass
class Fock(State):
    n: int = 1
    name = "fock"

    def __post_init__(self):
        self._grid = None

    def sample(self, rng, theta):
        if self.n == 0:
            return rng.normal(0, np.sqrt(VAC_VAR), np.shape(theta))
        if self.n == 1:   # |ψ_1|² = 2x² e^(−x²)/√π  →  x² ~ Gamma(3/2, 1)
            s = np.shape(theta)
            return np.sqrt(rng.gamma(1.5, 1.0, s)) * rng.choice([-1.0, 1.0], s)
        if self._grid is None:
            self._grid = _GridSampler(lambda x, th: hermite_function(self.n, x) ** 2, 1)
        return self._grid(rng, theta)

    def rho(self):
        r = np.zeros((self.n_fock, self.n_fock)); r[self.n, self.n] = 1
        return r

    def describe(self):
        return dict(type=self.name, n=self.n)


@dataclass
class Cat(State):
    """Schrödinger-Katze N(|α⟩ + s|−α⟩), s = +1 (gerade) / −1 (ungerade). Erweiterungspunkt (experimentell)."""
    alpha: float = 1.0
    parity: int = 1
    name = "cat"
    phase_sensitive = True
    n_theta: int = 256

    def __post_init__(self):
        self._grid = None

    def _psi(self, x, beta):
        # ⟨x|β⟩ für reell rotiertes β (Phase der Quadratur bereits in β enthalten)
        return np.pi ** -0.25 * np.exp(-(x - np.sqrt(2) * beta.real) ** 2 / 2
                                       + 1j * np.sqrt(2) * beta.imag * x - 1j * beta.real * beta.imag)

    def density(self, x, theta):
        b = self.alpha * np.exp(-1j * theta)
        psi = self._psi(x, b) + self.parity * self._psi(x, -b)
        return np.abs(psi) ** 2

    def sample(self, rng, theta):
        if self._grid is None:
            self._grid = _GridSampler(self.density, self.n_theta)
        return self._grid(rng, theta)

    def rho(self):
        n = np.arange(self.n_fock)
        c = np.exp(n * np.log(complex(self.alpha) + 0j) - 0.5 * gammaln(n + 1))
        c = c * (1 + self.parity * (-1.0) ** n)
        c = c / np.linalg.norm(c)
        return np.outer(c, np.conj(c))

    def describe(self):
        return dict(type=self.name, alpha=self.alpha, parity=self.parity)


def make_state(spec: dict | str) -> State:
    """Zustand aus Konfiguration, z. B. {type: coherent, alpha: 1.2, phi: 0.3}."""
    if isinstance(spec, str):
        spec = {"type": spec}
    spec = dict(spec)
    t = spec.pop("type", None)
    if t == "vacuum":
        return Vacuum()
    if t == "coherent":
        amp = float(spec.pop("alpha", 1.0)); phi = float(spec.pop("phi", 0.0))
        st: State = Coherent(alpha=amp * np.exp(1j * phi))
    elif t == "thermal":
        st = Thermal(nbar=float(spec.pop("nbar", 0.1)))
    elif t in ("fock", "fock1", "fock2"):
        n = int(spec.pop("n", 1 if t in ("fock", "fock1") else 2))
        st = Fock(n=n)
    elif t == "cat":
        st = Cat(alpha=float(spec.pop("alpha", 1.0)), parity=int(spec.pop("parity", 1)))
    else:
        raise ValueError(f"unbekannter Zustand {t!r} (vacuum, coherent, thermal, fock, fock1, fock2, cat)")
    if spec:
        raise ValueError(f"unbekannte Zustandsparameter {sorted(spec)}")
    return st
