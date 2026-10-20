"""Phasenkalibrierung der Piezo-Rampe im LO-Arm (ab 0.15): Rampenspannung V → θ aus kohärenten Referenzläufen K-θ.

Messprinzip: Im Lauf K-θ (``dataset_type = phase_reference``) liegt am Signaleingang ein schwacher kohärenter
Referenzstrahl (aus dem LO abgezweigt, Amplitude α). Dessen Quadraturmittel folgt ⟨x⟩ = A·cos θ mit
A = √(2η)·α; θ = 0 ist die Phase der Referenz (Bezugsrahmen der Auswertung).

Modell (je Rampenrichtung r = auf/ab, v = V/V_max):

    θ(V, r, t) = δ(t) + Σ_k c_{r,k}·v^k      (Polynom Grad ``deg``, Hysterese = getrennte Zweige)

δ(t) ist die Phasendrift: ein Versatz je Referenzlauf (δ des ersten Laufs = 0), dazwischen linear interpoliert,
außerhalb konstant. Gefittet werden alle K-θ-Records gemeinsam (nichtlineare kleinste Quadrate, Startwerte aus
einer Gittersuche), danach wird das Phasenrauschen σ_φ aus den Residuen geschätzt und mit
Var(x|θ) = ½ + A²σ_φ² sin²θ neu gewichtet.

Mehrdeutigkeit: Ein kohärenter Referenzstrahl legt θ nur bis auf das Vorzeichen fest (θ ↔ −θ entspricht p ↔ −p).
Festgelegt wird es durch ``sign``: +1 = θ steigt mit der Rampenspannung (Standard, Aufbau prüfen).

Fehler von θ je Record: Kovarianz der Fitparameter (Gradient von θ), plus Interpolationsfehler der Drift
zwischen den Referenzläufen als Brownsche Brücke mit q² aus den Versatzsprüngen (konservativ).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import least_squares

VAC = 0.5
THETA_SOURCES = {"none": 0, "ramp": 1, "locked": 2, "estimated": 3}
THETA_SOURCE_NAMES = {v: k for k, v in THETA_SOURCES.items()}


def ramp_direction(V: np.ndarray, waveform: str = "triangle", win: int = 5) -> np.ndarray:
    """Rampenrichtung je Record (+1 auf, −1 ab) aus der zeitlich geordneten Rampenspannung eines Laufs."""
    V = np.asarray(V, float)
    n = len(V)
    if waveform == "sawtooth" or n < 3:
        return np.ones(n, np.int8)
    d = np.empty(n)
    d[1:-1] = V[2:] - V[:-2]
    d[0], d[-1] = V[1] - V[0], V[-1] - V[-2]
    s = np.sign(d)
    if win > 1:                                    # gleitende Mehrheit (Rauschen, Umkehrpunkte)
        k = np.ones(win)
        s = np.sign(np.convolve(s, k, mode="same") + 1e-9 * s)
    s[s == 0] = 1
    return s.astype(np.int8)


@dataclass
class PhaseCalibration:
    deg: int
    branches: tuple                     # ("up", "down") bzw. ("up",)
    coef: dict                          # Zweig → Koeffizienten c_0..c_deg (rad)
    t_ref: np.ndarray                   # Mittelzeit der Referenzläufe (s, Sitzungsuhr)
    delta: np.ndarray                   # Versatz je Referenzlauf (rad), delta[0] = 0
    cov: np.ndarray                     # Kovarianz der Parameter [c…, δ_1…]
    A: float                            # Referenzamplitude (normierte Quadratur)
    V_max: float
    waveform: str
    sign: int = 1
    phase_noise_rad: float = 0.0
    q_drift: float = 0.0                # rad/√s, aus den Versatzsprüngen (Interpolationsfehler)
    quality: dict = field(default_factory=dict)
    ref_blocks: list = field(default_factory=list)

    # ---------------------------------------------------------------- Anwendung
    def _basis(self, V, d, t):
        v = np.asarray(V, float) / self.V_max
        P = np.vander(v, self.deg + 1, increasing=True)
        nb, K = len(self.branches), len(self.t_ref)
        G = np.zeros((len(v), nb * (self.deg + 1) + K - 1))
        bi = np.zeros(len(v), int) if nb == 1 else np.where(np.asarray(d) >= 0, 0, 1)
        for j in range(nb):
            m = bi == j
            G[m, j * (self.deg + 1):(j + 1) * (self.deg + 1)] = P[m]
        if K > 1:                                   # Interpolationsgewichte der Versätze δ_1 … δ_{K−1}
            W = np.zeros((len(v), K))
            t = np.asarray(t, float)
            for k in range(K):
                e = np.zeros(K); e[k] = 1.0
                W[:, k] = np.interp(t, self.t_ref, e)
            G[:, nb * (self.deg + 1):] = W[:, 1:]
        return G

    def _params(self):
        return np.concatenate([self.coef[b] for b in self.branches] + [self.delta[1:]])

    def theta(self, V, d, t, wrap: bool = True) -> tuple[np.ndarray, np.ndarray]:
        """θ (rad, modulo 2π bei ``wrap``) und Fehler σ_θ je Record."""
        G = self._basis(V, d, t)
        th = self.sign * (G @ self._params())
        var = np.einsum("ij,jk,ik->i", G, self.cov, G) if self.cov.size else np.zeros(len(th))
        t = np.asarray(t, float)
        if len(self.t_ref) > 1 and self.q_drift > 0:      # Brownsche Brücke zwischen den Referenzläufen
            tr = self.t_ref
            k = np.clip(np.searchsorted(tr, t) - 1, 0, len(tr) - 2)
            a, b = tr[k], tr[k + 1]
            inside = (t >= tr[0]) & (t <= tr[-1])
            br = np.where(inside, (t - a) * (b - t) / np.maximum(b - a, 1e-9),
                          np.minimum(np.abs(t - tr[0]), np.abs(t - tr[-1])))
            var = var + self.q_drift ** 2 * np.clip(br, 0, None)
        var = var + self.phase_noise_rad ** 2            # Phasenrauschen je Record (aus dem K-θ-Fit, nicht vorhersagbar)
        th = np.mod(th, 2 * np.pi) if wrap else th
        return th, np.sqrt(np.clip(var, 0, None))

    def to_dict(self) -> dict:
        return dict(deg=self.deg, branches=list(self.branches), coef={b: self.coef[b].tolist() for b in self.branches},
                    t_ref_s=self.t_ref.tolist(), delta_rad=self.delta.tolist(), A=self.A, V_max=self.V_max,
                    waveform=self.waveform, sign=self.sign, phase_noise_rad=self.phase_noise_rad,
                    q_drift_rad_per_sqrt_s=self.q_drift, quality=self.quality, ref_blocks=self.ref_blocks,
                    param_err=np.sqrt(np.clip(np.diag(self.cov), 0, None)).tolist())


# ---------------------------------------------------------------- Fit
def _grid_init(x, v, nbins=40):
    """Startwerte (A, c0, c1) eines linearen Modells θ = c0 + c1·v aus Bin-Mitteln (Gittersuche)."""
    e = np.linspace(v.min(), v.max() + 1e-12, nbins + 1)
    k = np.clip(np.digitize(v, e) - 1, 0, nbins - 1)
    cnt = np.bincount(k, minlength=nbins)
    ok = cnt >= 3
    m = np.bincount(k, x, nbins)[ok] / cnt[ok]
    vc = (0.5 * (e[1:] + e[:-1]))[ok]
    w = cnt[ok].astype(float)
    best = (np.inf, 1.0, 0.0, 1.0)
    for c1 in np.linspace(0.5, 40.0, 400):
        c0 = np.linspace(0, 2 * np.pi, 72, endpoint=False)[:, None]
        C = np.cos(c0 + c1 * vc[None, :])
        A = (C * m * w).sum(1) / np.maximum((C * C * w).sum(1), 1e-12)
        chi = ((m - A[:, None] * C) ** 2 * w).sum(1)
        j = int(np.argmin(chi))
        if chi[j] < best[0]:
            best = (chi[j], float(A[j]), float(c0[j, 0]), float(c1))
    _, A, c0, c1 = best
    if A < 0:
        A, c0 = -A, c0 + np.pi
    return A, c0, c1


def _offset_init(x, v, A, coef, deg):
    P = np.vander(v, deg + 1, increasing=True) @ coef
    grid = np.linspace(0, 2 * np.pi, 144, endpoint=False)
    chi = [np.sum((x - A * np.cos(P + g)) ** 2) for g in grid]
    return float(grid[int(np.argmin(chi))])


def fit_phase_calibration(refs: list[dict], V_max: float, waveform: str = "triangle", deg: int = 3,
                          sign: int = 1) -> PhaseCalibration:
    """``refs``: je K-θ-Lauf dict(block, x (normiert), V (V), d (Richtung), t (s)). Gibt die Kalibrierung zurück."""
    refs = [r for r in refs if len(r["x"]) >= 50]
    if not refs:
        raise ValueError("keine K-θ-Records für die Phasenkalibrierung")
    branches = ("up",) if waveform == "sawtooth" else ("up", "down")
    nb, K, nc = len(branches), len(refs), deg + 1
    t_ref = np.array([float(np.mean(r["t"])) for r in refs])
    o = np.argsort(t_ref); refs = [refs[i] for i in o]; t_ref = t_ref[o]
    x = np.concatenate([r["x"] for r in refs]); v = np.concatenate([r["V"] for r in refs]) / V_max
    d = np.concatenate([np.asarray(r["d"]) for r in refs]); t = np.concatenate([r["t"] for r in refs])
    run = np.concatenate([np.full(len(r["x"]), k) for k, r in enumerate(refs)])
    bi = np.zeros(len(x), int) if nb == 1 else np.where(d >= 0, 0, 1)
    # Startwerte: linear je Zweig im ersten Lauf, Versätze der übrigen Läufe per Gitter
    m0 = run == 0
    A0, c00, c10 = _grid_init(x[m0 & (bi == 0)], v[m0 & (bi == 0)])
    coef0 = {}
    for j, b in enumerate(branches):
        c = np.zeros(nc); c[1] = c10
        mm = m0 & (bi == j)
        c[0] = c00 if j == 0 else _offset_init(x[mm], v[mm], A0, np.r_[0.0, c10, np.zeros(nc - 2)], deg)
        coef0[b] = c
    delta0 = np.zeros(K)
    for k in range(1, K):
        mk = (run == k) & (bi == 0)
        delta0[k] = _offset_init(x[mk], v[mk], A0, coef0["up"], deg) - coef0["up"][0]
    Pv = np.vander(v, nc, increasing=True)

    def unpack(p):
        A = p[0]; cs = p[1:1 + nb * nc].reshape(nb, nc); dl = np.r_[0.0, p[1 + nb * nc:]]
        return A, cs, dl

    def theta_of(p):
        A, cs, dl = unpack(p)
        return np.einsum("ij,ij->i", Pv, cs[bi]) + dl[run]

    sig = np.full(len(x), np.sqrt(VAC))

    def resid(p):
        return (x - p[0] * np.cos(theta_of(p))) / sig

    p0 = np.concatenate([[A0]] + [coef0[b] for b in branches] + [delta0[1:]])
    sol = least_squares(resid, p0, method="lm", x_scale="jac")
    # Phasenrauschen aus den Residuen, neu gewichten
    A = sol.x[0]; th = theta_of(sol.x)
    r2 = (x - A * np.cos(th)) ** 2
    s2 = (A * np.sin(th)) ** 2
    # r² = v0 + σ_φ²·(A sin θ)²: Regression mit freiem Achsenabschnitt v0, damit ein kleiner Fehler der
    # Vakuumnormierung nicht als Phasenrauschen erscheint; nur signifikante Werte (> 3σ) werden übernommen.
    X = np.column_stack([np.ones_like(s2), s2])
    beta, *_ = np.linalg.lstsq(X, r2, rcond=None)
    res = r2 - X @ beta
    try:
        cb = np.linalg.inv(X.T @ X) * float(np.mean(res ** 2))
        sphi2_err = float(np.sqrt(max(cb[1, 1], 0)))
    except np.linalg.LinAlgError:
        sphi2_err = float("inf")
    sphi2_raw = float(beta[1])
    sphi2 = max(0.0, sphi2_raw) if sphi2_raw > 3 * sphi2_err else 0.0
    sig = np.sqrt(VAC + sphi2 * s2)
    sol = least_squares(resid, sol.x, method="lm", x_scale="jac")
    J = sol.jac
    ndof = max(1, len(x) - len(sol.x))
    chi2 = float(np.sum(sol.fun ** 2))
    try:
        C = np.linalg.inv(J.T @ J) * max(1.0, chi2 / ndof)
    except np.linalg.LinAlgError:
        C = np.full((len(sol.x), len(sol.x)), np.nan)
    A, cs, dl = unpack(sol.x)
    if A < 0:                                       # Vorzeichen der Amplitude: θ → θ + π
        A = -A; cs = cs.copy(); cs[:, 0] += np.pi
    # 2π-Mehrdeutigkeiten: Zweige und Versätze stetig wählen
    vv = np.linspace(0, 1, 101); Pg = np.vander(vv, nc, increasing=True)
    for j in range(1, nb):
        diff = np.mean(Pg @ cs[j] - Pg @ cs[0])
        cs[j, 0] -= 2 * np.pi * np.round(diff / (2 * np.pi))
    cs[0, 0] = np.mod(cs[0, 0], 2 * np.pi) if nb == 1 else cs[0, 0]
    shift = cs[0, 0] - np.mod(cs[0, 0], 2 * np.pi)
    cs[:, 0] -= shift
    for k in range(1, K):
        dl[k] -= 2 * np.pi * np.round((dl[k] - dl[k - 1]) / (2 * np.pi))
    coef = {b: cs[j] for j, b in enumerate(branches)}
    covp = C[1:, 1:]
    # Güte
    up = Pg @ cs[0]
    lin = np.polyval(np.polyfit(vv, up, 1), vv)
    q = dict(n_records=int(len(x)), chi2=chi2, ndof=ndof, chi2_ndof=chi2 / ndof,
             stroke_rad=float(up[-1] - up[0]), nonlinearity_rad=float(np.max(np.abs(up - lin))),
             hysteresis_rad=float(np.max(np.abs(Pg @ cs[1] - up))) if nb > 1 else 0.0,
             v_range=[float(v.min()), float(v.max())], A=float(A), A_err=float(np.sqrt(max(C[0, 0], 0))),
             drift_rad=dl.tolist(), phase_noise_rad=float(np.sqrt(sphi2)),
             phase_noise_var_fit=sphi2_raw, phase_noise_var_err=sphi2_err)
    warn = []
    if q["chi2_ndof"] > 1.5:
        warn.append(f"K-θ-Fit: χ²/ndof = {q['chi2_ndof']:.2f} (Modell passt schlecht)")
    if abs(q["stroke_rad"]) < 2 * np.pi:
        warn.append(f"Piezo-Hub {q['stroke_rad']:.2f} rad < 2π: Phasenbereich nicht vollständig")
    if A < 1.0:
        warn.append(f"Referenzamplitude A = {A:.2f} klein: θ-Fit unsicher")
    q["warnings"] = warn
    # Zufallsdrift q (rad/√s) für die Brownsche Brücke: die lineare Interpolation nimmt die gleichmäßige Drift heraus,
    # daher ab drei Referenzläufen aus den zweiten Differenzen; bei zwei Läufen konservativ aus der ersten Differenz.
    qd = 0.0
    dt = np.diff(t_ref)
    if K > 2:
        sl = np.diff(dl) / np.maximum(dt, 1e-9)
        d2 = np.diff(sl)
        i1, i2 = 1 / np.maximum(dt[:-1], 1e-9), 1 / np.maximum(dt[1:], 1e-9)
        w = i1 + i2
        # Messfehler der Versätze δ (Fit-Kovarianz) abziehen, sonst wird q bei kurzen K-θ-Läufen überschätzt
        s2 = float(np.mean(np.clip(np.diag(covp)[-(K - 1):], 0, None))) if covp.size else 0.0
        vm = s2 * (i1 ** 2 + (i1 + i2) ** 2 + i2 ** 2)
        qd = float(np.sqrt(max(0.0, np.mean((d2 ** 2 - vm) / w))))
        q["drift_estimate"] = "zweite Differenzen"
    elif K == 2:
        qd = float(abs(dl[1] - dl[0]) / np.sqrt(max(dt[0], 1e-9)))
        q["drift_estimate"] = "erste Differenz (konservativ)"
    q["q_drift_rad_per_sqrt_s"] = qd
    return PhaseCalibration(deg=deg, branches=branches, coef=coef, t_ref=t_ref, delta=dl, cov=covp, A=float(A),
                            V_max=float(V_max), waveform=waveform, sign=int(sign), phase_noise_rad=float(np.sqrt(sphi2)),
                            q_drift=qd, quality=q, ref_blocks=[r.get("block", "") for r in refs])
