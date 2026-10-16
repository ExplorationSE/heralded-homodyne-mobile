"""Phasenmodell der Homodynmessung (ED-11: phasengemittelt **und** bekannte Phase θ).

θ ist die Phase zwischen Lokaloszillator und Signal zum Modenzeitpunkt (wahre Zeit). Modi:

* ``random`` – θ gleichverteilt (phasengemittelte Messung, Standard für das heralded Einzelphoton);
* ``fixed``  – θ = θ0 (stabilisierte Phase);
* ``ramp``   – Sägezahn θ(t) = θ0 + 2π·frac((t − t0)·f_ramp) (Piezo-Rampe), Beginn jeder Periode als Harp-Marker;
* ``steps``  – Folge fester Werte mit Verweildauer, Beginn jeder Stufe als Harp-Marker.

Die Software kennt θ im Messbetrieb nur aus Metadaten bzw. Markern (Gesamtplan F9); der Simulator legt die
wahre Phase je Mode in der Wahrheitsdatei ab.
"""
from __future__ import annotations

import numpy as np

from .config import PhaseCfg


class PhaseModel:
    def __init__(self, cfg: PhaseCfg, t0: float, duration: float):
        self.cfg, self.t0, self.T = cfg, t0, duration

    def theta(self, t: np.ndarray, rng: np.random.Generator) -> np.ndarray:
        t = np.asarray(t, dtype=float)
        c = self.cfg
        if c.mode == "random":
            return rng.uniform(0, 2 * np.pi, t.shape)
        if c.mode == "fixed":
            return np.full(t.shape, float(c.theta0))
        if c.mode == "ramp":
            return np.mod(c.theta0 + 2 * np.pi * np.mod((t - self.t0) * c.ramp_Hz, 1.0), 2 * np.pi)
        if c.mode == "steps":
            k = np.floor((t - self.t0) / c.dwell_s).astype(int) % len(c.steps)
            return np.mod(np.asarray(c.steps, float)[k] + c.theta0, 2 * np.pi)
        raise ValueError(c.mode)

    def reference_times(self) -> np.ndarray:
        """Wahre Zeiten der Phasenreferenz-Marker (Beginn jeder Rampe bzw. Stufe)."""
        c = self.cfg
        if c.mode == "ramp" and c.ramp_Hz > 0:
            return self.t0 + np.arange(0, self.T, 1.0 / c.ramp_Hz)
        if c.mode == "steps":
            return self.t0 + np.arange(0, self.T, c.dwell_s)
        return np.empty(0)

    def metadata(self) -> dict:
        c = self.cfg
        return dict(phase_mode=c.mode, phase_theta0_rad=float(c.theta0), phase_ramp_Hz=float(c.ramp_Hz),
                    phase_steps_rad=list(map(float, c.steps)), phase_dwell_s=float(c.dwell_s),
                    phase_marker_bit=int(c.marker_bit))
