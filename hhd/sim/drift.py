"""Driftmodell über eine Sitzung und Allan-Abweichung (Laufplan, Gesamtplan v1.5 R15, Vorschlag M1).

Relative LO-Leistung, Zustandseffizienz η und Verstärkung des LO-Monitors (Kanal B) als Funktion der wahren
Zeit t (ab Sitzungsbeginn t_s)::

    P_LO(t) = (1 + a_LO·(t − t_s)) · (1 + w_LO(t)) · (1 + A_LO·sin(2π(t − t_s)/T_LO + φ_LO))
    η(t)/η  = (1 + a_η ·(t − t_s)) · (1 + w_η (t)) · (1 + A_η ·sin(2π(t − t_s)/T_η  + φ_η ))
    g_B(t)  = 1 + w_B(t)

``a`` ist die lineare Drift (``drift.lo_rel_per_s`` usw., bisheriges Modell), ``w`` eine Irrfahrt
(Random Walk) mit ⟨[w(t+τ) − w(t)]²⟩ = q²·τ (``q`` = ``*_rw_per_sqrt_s``), ``A``/``T`` eine periodische
Schwankung (z. B. Klimaanlage) mit zufälliger Phase φ. ``g_B`` ist die Verstärkungsdrift des LO-Monitors
(Strahlteiler-/Kopplerverhältnis, Polarisation, Photodiodentemperatur), die eine Normierung mit dem Monitor
verfälscht. Alle stochastischen Anteile sind aus (0 = aus), dann gilt das bisherige lineare Modell bitgleich.

Allan-Abweichung (überlappend) für die Stabilitätsmessung AP14: Für eine Irrfahrt gilt σ_A(τ) = q·√(τ/3),
für weißes Rauschen σ_A(τ) = σ₁/√(τ/τ₀), für eine lineare Drift a σ_A(τ) = a·τ/√2.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class _Component:
    lin: float = 0.0
    rw: float = 0.0
    sin_amp: float = 0.0
    sin_period: float = 0.0


class DriftPath:
    """Eine Realisierung der Drift über die Sitzung (Stützstellen im Abstand ``grid_dt_s``, linear interpoliert)."""

    def __init__(self, drift_cfg, lo_monitor_cfg, t_start: float, t_end: float,
                 rng: np.random.Generator | None = None):
        d = drift_cfg
        self.t_s = float(t_start)
        self.comp = dict(
            lo=_Component(d.lo_rel_per_s, d.lo_rw_per_sqrt_s, d.lo_sin_amp, d.lo_sin_period_s),
            eta=_Component(d.eta_rel_per_s, d.eta_rw_per_sqrt_s, d.eta_sin_amp, d.eta_sin_period_s),
            mon=_Component(0.0, getattr(lo_monitor_cfg, "gain_rw_per_sqrt_s", 0.0), 0.0, 0.0),
        )
        self.stochastic = any(c.rw > 0 or c.sin_amp != 0 for c in self.comp.values())
        dt = float(d.grid_dt_s)
        n = max(2, int(np.ceil((t_end - t_start) / dt)) + 2)
        self.tg = t_start + dt * np.arange(n)
        self.paths: dict[str, np.ndarray] = {}
        self.phases: dict[str, float] = {}
        rng = rng if rng is not None else np.random.default_rng(0)
        for name in ("lo", "eta", "mon"):            # feste Reihenfolge der Zufallszahlen
            c = self.comp[name]
            w = np.zeros(n)
            if c.rw > 0:
                w[1:] = np.cumsum(rng.normal(0.0, c.rw * np.sqrt(dt), n - 1))
            phi = float(rng.uniform(0, 2 * np.pi)) if c.sin_amp else 0.0
            self.paths[name] = w
            self.phases[name] = phi

    def factor(self, name: str, t) -> np.ndarray:
        c = self.comp[name]
        t = np.asarray(t, float)
        x = 1.0 + c.lin * (t - self.t_s)
        if c.rw > 0:
            x = x * (1.0 + np.interp(t, self.tg, self.paths[name]))
        if c.sin_amp and c.sin_period > 0:
            x = x * (1.0 + c.sin_amp * np.sin(2 * np.pi * (t - self.t_s) / c.sin_period + self.phases[name]))
        return x

    def lo(self, t):
        return self.factor("lo", t)

    def eta(self, t):
        return self.factor("eta", t)

    def monitor_gain(self, t):
        return self.factor("mon", t)

    def truth(self) -> dict:
        return dict(t_grid=self.tg, lo=self.lo(self.tg), eta_factor=self.eta(self.tg),
                    monitor_gain=self.monitor_gain(self.tg))


# ------------------------------------------------------------------------------------------- Allan-Abweichung
def allan_deviation(y, dt: float, taus=None):
    """Überlappende Allan-Abweichung einer gleichabständigen Zeitreihe ``y`` (relative Werte, Abstand ``dt``).

    Rückgabe ``(tau, adev, n)`` mit ``n`` = Zahl der Differenzen je τ. Ohne ``taus``: oktavweise ab ``dt``.
    """
    y = np.asarray(y, float)
    N = len(y)
    if N < 3:
        raise ValueError("Zeitreihe zu kurz für eine Allan-Abweichung")
    if taus is None:
        ms = np.unique(np.floor(2.0 ** np.arange(0, np.log2(N / 2) + 1e-9)).astype(int))
    else:
        ms = np.unique(np.maximum(1, np.rint(np.asarray(taus, float) / dt).astype(int)))
    ms = ms[ms <= (N - 1) // 2]
    c = np.concatenate([[0.0], np.cumsum(y)])
    tau, adev, nn = [], [], []
    for m in ms:
        avg = (c[m:] - c[:-m]) / m                     # gleitende Mittelwerte der Länge m
        d = avg[m:] - avg[:-m]
        if len(d) < 1:
            continue
        tau.append(m * dt); adev.append(np.sqrt(0.5 * np.mean(d ** 2))); nn.append(len(d))
    return np.array(tau), np.array(adev), np.array(nn)


def fit_noise_model(tau, adev, n=None) -> dict:
    """Passt σ_A²(τ) = h_w/τ + q²·τ/3 + (a·τ)²/2 (weiß + Irrfahrt + lineare Drift) nicht-negativ an.

    Liefert ``white`` (σ bei τ = 1 s), ``q_rw`` (1/√s), ``lin`` (1/s) – die Größen, aus denen
    :func:`hhd.sim.plan.adapt_plan` die Laufdauern ableitet.
    """
    from scipy.optimize import nnls
    tau = np.asarray(tau, float); a2 = np.asarray(adev, float) ** 2
    w = np.ones_like(tau) if n is None else np.sqrt(np.asarray(n, float))
    A = np.stack([1 / tau, tau / 3, tau ** 2 / 2], 1) / a2[:, None]   # relative Fehler gewichten
    coef, _ = nnls(A * w[:, None], np.ones_like(tau) * w)
    hw, q2, a2c = coef
    return dict(white=float(np.sqrt(hw)), q_rw=float(np.sqrt(q2)), lin=float(np.sqrt(a2c)))


# ------------------------------------------------------------------------------------------- Szenarien
#: Driftszenarien für die Laufplan-Studie [Annahme]. Werte als relative Größen; "pro √h" = q·√3600.
SCENARIOS = {
    "keine": dict(),
    "gut": dict(lo_rw_per_sqrt_s=0.001 / 60, eta_rw_per_sqrt_s=0.0005 / 60, gain_rw_per_sqrt_s=0.0002 / 60),
    "typisch": dict(lo_rw_per_sqrt_s=0.005 / 60, lo_sin_amp=0.003, lo_sin_period_s=1200.0,
                    eta_rw_per_sqrt_s=0.002 / 60, gain_rw_per_sqrt_s=0.001 / 60),
    "schlecht": dict(lo_rw_per_sqrt_s=0.02 / 60, lo_sin_amp=0.01, lo_sin_period_s=900.0, lo_rel_per_s=0.01 / 3600,
                     eta_rw_per_sqrt_s=0.01 / 60, eta_sin_amp=0.005, eta_sin_period_s=900.0,
                     gain_rw_per_sqrt_s=0.003 / 60),
}


def scenario_overrides(name: str) -> dict:
    """Konfigurations-Überschreibung ``{"drift": ..., "lo_monitor": ...}`` für ein Szenario aus :data:`SCENARIOS`."""
    s = dict(SCENARIOS[name])
    g = s.pop("gain_rw_per_sqrt_s", 0.0)
    return {"drift": s, "lo_monitor": {"gain_rw_per_sqrt_s": g}}
