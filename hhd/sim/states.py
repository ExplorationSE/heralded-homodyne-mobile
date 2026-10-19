"""Zustandsmodelle für die Quadratur einer temporalen Mode.

Konvention: x_θ = (a e^{−iθ} + a† e^{iθ})/√2, Vakuumvarianz 1/2, W(0,0) des Vakuums = 1/π.

Verluste (Effizienz η) werden **exakt** als Strahlteiler mit Vakuum modelliert:
x_mess = √η·x + √(1−η)·v, v ~ N(0, 1/2). Für die Randverteilungen der Homodynmessung ist das
gleichbedeutend mit dem Verlustkanal auf ρ (z. B. η|1⟩⟨1| + (1−η)|0⟩⟨0| für das Einzelphoton).

Sollwerte (Fock-Diagonale, W(0,0), ⟨x⟩, Var x) liefert ``expected(eta)`` aus der Dichtematrix
(abgeschnittene Fock-Basis, Verlustkanal über Binomialkoeffizienten). Für phasengemittelte Messungen (Phase
zufällig, Auswertung v1.0) gilt ``var_x_phase_avg = ⟨n⟩ + ½``; p_n und W(0,0) hängen nicht von der Phase ab.
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
        mean_n = float(np.sum(np.arange(len(p)) * p))
        return dict(p_n=p[:12].tolist(), W00=float(np.sum(p * (-1.0) ** np.arange(len(p))) / np.pi),
                    mean_x=mean, var_x=var, mean_n=mean_n, var_x_phase_avg=mean_n + 0.5,
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


@dataclass
class Squeezed(State):
    """Gequetschtes Vakuum S(r)|0⟩, Quetschung in x_θ bei θ = φ: Var x_φ = e^(−2r)/2, Var x_(φ+π/2) = e^(2r)/2."""
    r: float = 0.345
    phi: float = 0.0
    name = "squeezed"
    phase_sensitive = True

    @staticmethod
    def r_from_dB(db: float) -> float:
        return float(np.log(10 ** (db / 10)) / 2)

    def sample(self, rng, theta):
        th = np.asarray(theta, dtype=float)
        v = VAC_VAR * (np.cosh(2 * self.r) - np.sinh(2 * self.r) * np.cos(2 * (th - self.phi)))
        return rng.normal(0, 1, th.shape) * np.sqrt(v)

    def coeffs(self) -> np.ndarray:
        m = np.arange(self.n_fock // 2)
        lc = 0.5 * gammaln(2 * m + 1) - m * np.log(2) - gammaln(m + 1)
        c = np.zeros(self.n_fock, complex)
        c[2 * m] = np.exp(lc) * (-np.tanh(self.r) * np.exp(2j * self.phi)) ** m / np.sqrt(np.cosh(self.r))
        return c / np.linalg.norm(c)

    def rho(self):
        c = self.coeffs()
        return np.outer(c, np.conj(c))

    def describe(self):
        return dict(type=self.name, r=self.r, dB=float(10 * np.log10(np.exp(2 * self.r))), phi=self.phi)


class FockSuperposition(State):
    """Reiner Zustand Σ c_n |n⟩ (abgeschnitten). Quadraturdichte |Σ c_n e^(−inθ) ψ_n(x)|², Abtastung auf einem
    θ-Gitter (256 Stützstellen). Grundlage für |0⟩+|1⟩ und das photonensubtrahierte gequetschte Vakuum."""
    name = "superposition"
    n_theta = 256

    def __init__(self, c, name: str | None = None, desc: dict | None = None):
        c = np.asarray(c, dtype=complex)
        self.c = c / np.linalg.norm(c)
        self.n_fock = max(len(self.c), 2)
        nz = np.flatnonzero(np.abs(self.c) > 1e-12)
        self.phase_sensitive = bool(len(nz) > 1)
        if name:
            self.name = name
        self._desc = desc or {}
        self._grid = None
        self._H = None

    def density(self, x, theta):
        n = len(self.c)
        if self._H is None or self._H.shape[1] != len(x):
            self._H = np.array([hermite_function(k, x) for k in range(n)])
        amp = (self.c * np.exp(-1j * np.arange(n) * theta)) @ self._H
        return np.abs(amp) ** 2

    def sample(self, rng, theta):
        if self._grid is None:
            self._grid = _GridSampler(self.density, self.n_theta if self.phase_sensitive else 1)
        return self._grid(rng, theta)

    def rho(self):
        c = np.zeros(self.n_fock, complex); c[:len(self.c)] = self.c
        return np.outer(c, np.conj(c))

    def describe(self):
        return dict(type=self.name, **self._desc)


def photon_subtracted_squeezed(r: float, n_fock: int = 40) -> FockSuperposition:
    """a S(r)|0⟩ (normiert): „Kätzchen“, ungerade, nahe einer ungeraden Katze mit kleinem α."""
    sq = Squeezed(r=r); sq.n_fock = n_fock
    c = sq.coeffs()
    d = np.zeros(n_fock, complex)
    d[:-1] = np.sqrt(np.arange(1, n_fock)) * c[1:]
    return FockSuperposition(d, name="kitten", desc=dict(r=r, dB=float(10 * np.log10(np.exp(2 * r)))))


# Voreinstellungen für den geheraldeten Zustand (``source.heralded_state``) und die Laufpläne „kurz_<name>“
STATE_PRESETS: dict[str, dict] = {
    "fock1": dict(spec=dict(type="fock", n=1), label="Einzelphoton |1⟩"),
    "fock2": dict(spec=dict(type="fock", n=2), label="Fock |2⟩"),
    "fock3": dict(spec=dict(type="fock", n=3), label="Fock |3⟩"),
    "fock01": dict(spec=dict(type="superposition", c=[1.0, 1.0]), label="Überlagerung (|0⟩+|1⟩)/√2"),
    "cat_even_1": dict(spec=dict(type="cat", alpha=1.0, parity=1), label="Katze gerade α=1"),
    "cat_odd_1": dict(spec=dict(type="cat", alpha=1.0, parity=-1), label="Katze ungerade α=1"),
    "cat_even_2": dict(spec=dict(type="cat", alpha=2.0, parity=1), label="Katze gerade α=2"),
    "cat_odd_2": dict(spec=dict(type="cat", alpha=2.0, parity=-1), label="Katze ungerade α=2"),
    "kitten_3dB": dict(spec=dict(type="kitten", dB=3.0), label="Kätzchen (photonensubtr. gequetscht, 3 dB)"),
    "coherent_1": dict(spec=dict(type="coherent", alpha=1.0), label="kohärent |α=1⟩"),
    "squeezed_3dB": dict(spec=dict(type="squeezed", dB=3.0), label="gequetschtes Vakuum 3 dB"),
    "squeezed_6dB": dict(spec=dict(type="squeezed", dB=6.0), label="gequetschtes Vakuum 6 dB"),
    "thermal_0.5": dict(spec=dict(type="thermal", nbar=0.5), label="thermisch n̄=0,5"),
}

_CACHE: dict[str, State] = {}


def state_info(spec: dict | str) -> dict:
    """Beschreibung für Metadaten und Auswertung: Typ, Anzeigename, phasenempfindlich, ⟨n⟩ (verlustfrei)."""
    key = spec if isinstance(spec, str) else None
    st = make_state(spec)
    e = st.expected(1.0)
    label = STATE_PRESETS[key]["label"] if key in STATE_PRESETS else str(st.describe())
    return dict(st.describe(), preset=key or "", label=label, phase_sensitive=bool(st.phase_sensitive),
                mean_n_ideal=float(e["mean_n"]), W00_ideal=float(e["W00"]),
                p_n_ideal=[round(v, 8) for v in e["p_n"]])


def make_state(spec: dict | str) -> State:
    """Zustand aus Konfiguration, z. B. {type: coherent, alpha: 1.2, phi: 0.3} oder Voreinstellung ``"cat_even_2"``.

    Voreinstellungen werden zwischengespeichert (Abtastgitter nur einmal je Prozess)."""
    if isinstance(spec, str):
        if spec in STATE_PRESETS:
            if spec not in _CACHE:
                _CACHE[spec] = make_state(STATE_PRESETS[spec]["spec"])
            return _CACHE[spec]
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
    elif t in ("fock", "fock1", "fock2", "fock3"):
        n = int(spec.pop("n", {"fock": 1, "fock1": 1, "fock2": 2, "fock3": 3}[t]))
        st = Fock(n=n)
    elif t == "cat":
        st = Cat(alpha=float(spec.pop("alpha", 1.0)), parity=int(spec.pop("parity", 1)))
    elif t == "squeezed":
        r = float(spec.pop("r")) if "r" in spec else Squeezed.r_from_dB(float(spec.pop("dB", 3.0)))
        st = Squeezed(r=r, phi=float(spec.pop("phi", 0.0)))
    elif t == "kitten":
        r = float(spec.pop("r")) if "r" in spec else Squeezed.r_from_dB(float(spec.pop("dB", 3.0)))
        st = photon_subtracted_squeezed(r)
    elif t == "superposition":
        c = spec.pop("c")
        st = FockSuperposition(c, desc=dict(c=[float(np.real(v)) for v in c]))
    else:
        raise ValueError(f"unbekannter Zustand {t!r} (vacuum, coherent, thermal, fock, fock1, fock2, fock3, cat, "
                         f"squeezed, kitten, superposition oder {', '.join(STATE_PRESETS)})")
    if spec:
        raise ValueError(f"unbekannte Zustandsparameter {sorted(spec)}")
    return st
