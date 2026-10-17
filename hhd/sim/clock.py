"""Uhrmodell des Digitizers relativ zur Harp (wahre Zeitachse = Harp-Zeitbasis).

T_D(t) = (1 + ε)(t − t_arm) + φ(t), φ = Phasen-Irrfahrt mit ⟨[φ(t+τ) − φ(t)]²⟩ = D·τ (Vorbericht 2, Kap. 2.4).
Mit gemeinsamer 10-MHz-Referenz (ED-7) gilt ε = 0 und φ = 0.
"""
from __future__ import annotations

import numpy as np

from .config import ClockCfg


class DigitizerClock:
    def __init__(self, cfg: ClockCfg, t_end: float, rng: np.random.Generator):
        self.eps = 0.0 if cfg.shared_10MHz else cfg.eps
        self.D = 0.0 if cfg.shared_10MHz else cfg.rw_D
        self.t_arm = cfg.t_arm_s
        self.dt = cfg.grid_dt
        n = int(np.ceil(t_end / self.dt)) + 2
        self.tg = np.arange(n) * self.dt
        if self.D > 0:
            steps = rng.normal(0, np.sqrt(self.D * self.dt), n)
            steps[0] = 0
            self.phig = np.cumsum(steps)
        else:
            self.phig = np.zeros(n)

    def phi(self, t):
        return np.interp(t, self.tg, self.phig)

    def __call__(self, t):
        t = np.asarray(t, dtype=float)
        return (1 + self.eps) * (t - self.t_arm) + self.phi(t)

    def inverse(self, TD):
        """wahre Zeit zu Digitizer-Zeit (Fixpunktiteration; φ ist klein und glatt)."""
        TD = np.asarray(TD, dtype=float)
        t = TD / (1 + self.eps) + self.t_arm
        for _ in range(3):
            t = (TD - self.phi(t)) / (1 + self.eps) + self.t_arm
        return t
