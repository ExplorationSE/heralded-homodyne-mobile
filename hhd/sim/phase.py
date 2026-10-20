"""Phasenmodell der Homodynmessung (ED-11: phasengemittelt **und** bekannte Phase θ).

θ ist die Phase zwischen Lokaloszillator und Signal zum Modenzeitpunkt (wahre Zeit). Modi:

* ``random`` – θ gleichverteilt (phasengemittelte Messung, Standard für das heralded Einzelphoton);
* ``fixed``  – θ = θ0 (stabilisierte Phase);
* ``ramp``   – Sägezahn θ(t) = θ0 + 2π·frac((t − t_ref), t_ref = Blockbeginn + 1 µs·f_ramp) (Piezo-Rampe), Beginn jeder Periode als Harp-Marker;
* ``steps``  – Folge fester Werte mit Verweildauer, Beginn jeder Stufe als Harp-Marker;
* ``piezo``  – Piezo-Rampe im LO-Arm (ab 0.15): Rampenspannung u(t) (Dreieck oder Sägezahn, läuft über die ganze
  Sitzung durch, Sitzungszeit t), θ = θ0 + 2π·Hub·g_r(u) + Drift(t) + Rauschen mit g_r(u) = u + a·u(u−1) ∓ h·4u(1−u)
  (Zweig r: auf −, ab +), Drift linear + Irrfahrt (deterministisch aus dem Sitzungs-Seed, auch im Datenstrom
  scheibenunabhängig), weißes Phasenrauschen je Mode. Die Rampenspannung wird je Record vom Hilfs-ADC
  aufgezeichnet (``ramp_channel = aux_adc`` → ``records/ramp_raw``); Rampenbeginn als Harp-Marker.

Die Software kennt θ im Messbetrieb nur aus Metadaten bzw. Markern (Gesamtplan F9); der Simulator legt die
wahre Phase je Mode in der Wahrheitsdatei ab.
"""
from __future__ import annotations

import numpy as np

from .config import PhaseCfg


_WALK_CACHE: dict = {}


def phase_walk(t: np.ndarray, seed: int, q: float, dt: float = 0.05, chunk: int = 4096) -> np.ndarray:
    """Irrfahrt der Phase φ_rw(t) (Sitzungszeit t ≥ 0, φ_rw(0) = 0), deterministisch aus ``seed`` und unabhängig
    davon, in welchen Stücken t abgefragt wird (Datei- und Datenstrom-Simulator liefern dieselbe Phase)."""
    t = np.asarray(t, float)
    if q <= 0 or t.size == 0:
        return np.zeros(t.shape)
    key = (int(seed), float(q), float(dt), int(chunk))
    chunks = _WALK_CACHE.setdefault(key, [])
    need = int(np.max(t) / dt) + 2
    while len(chunks) * chunk < need + 1:
        c = len(chunks)
        rng = np.random.default_rng(np.random.SeedSequence([int(seed) & 0xFFFFFFFF, 0x5EED, c]))
        start = chunks[-1][-1] if chunks else 0.0
        inc = rng.normal(0.0, q * np.sqrt(dt), chunk)
        inc[0] = 0.0 if not chunks else inc[0]
        chunks.append(start + np.cumsum(inc))
    grid = np.concatenate(chunks)
    k = np.clip(t / dt, 0, len(grid) - 1.000001)
    i = np.floor(k).astype(np.int64); w = k - i
    return grid[i] * (1 - w) + grid[i + 1] * w


def piezo_ramp(c: PhaseCfg, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Rampenspannung relativ u ∈ [0, 1] und Richtung (+1 auf, −1 ab) zur Sitzungszeit t."""
    f = np.mod(np.asarray(t, float) * c.piezo_Hz, 1.0)
    if c.piezo_waveform == "sawtooth":
        return f, np.ones(f.shape, np.int8)
    up = f < 0.5
    return np.where(up, 2 * f, 2 - 2 * f), np.where(up, 1, -1).astype(np.int8)


def piezo_shape(c: PhaseCfg, u: np.ndarray, d: np.ndarray) -> np.ndarray:
    """g_r(u): Phasenhub (in Einheiten 2π·Hub) mit Nichtlinearität und Hysterese je Zweig."""
    u = np.asarray(u, float)
    return u + c.piezo_nonlin * u * (u - 1) - np.asarray(d, float) * c.piezo_hysteresis * 4 * u * (1 - u)


class PhaseModel:
    REF_LEAD = 1e-6

    def __init__(self, cfg: PhaseCfg, t0: float, duration: float, seed: int = 0, session_start: float = 0.0):
        self.cfg, self.t0, self.T = cfg, t0, duration
        self.seed, self.session_start = seed, session_start
        # Erste Referenz 1 µs nach Blockbeginn, damit sie trotz Marker-Zeitfehler (±100 ns) sicher in der PTU-Datei liegt.
        self.t_ref = t0 + self.REF_LEAD

    def theta(self, t: np.ndarray, rng: np.random.Generator) -> np.ndarray:
        t = np.asarray(t, dtype=float)
        c = self.cfg
        if c.mode == "random":
            return rng.uniform(0, 2 * np.pi, t.shape)
        if c.mode == "fixed":
            return np.full(t.shape, float(c.theta0))
        if c.mode == "ramp":
            return np.mod(c.theta0 + 2 * np.pi * np.mod((t - self.t_ref) * c.ramp_Hz, 1.0), 2 * np.pi)
        if c.mode == "steps":
            k = np.floor((t - self.t_ref) / c.dwell_s).astype(int) % len(c.steps)
            return np.mod(np.asarray(c.steps, float)[k] + c.theta0, 2 * np.pi)
        if c.mode == "piezo":
            th = self.theta_clean(t)
            if c.noise_rad > 0:
                th = th + rng.normal(0.0, c.noise_rad, t.shape)
            return np.mod(th, 2 * np.pi)
        raise ValueError(c.mode)

    def theta_clean(self, t: np.ndarray) -> np.ndarray:
        """Piezo: θ ohne schnelles Rauschen (nicht modulo 2π) zur wahren Zeit t (s, Sitzungsuhr)."""
        c = self.cfg
        ts = np.asarray(t, float) - self.session_start
        u, d = piezo_ramp(c, ts)
        return (c.theta0 + 2 * np.pi * c.piezo_stroke_lambda * piezo_shape(c, u, d) + c.drift_rad_per_s * ts
                + phase_walk(ts, self.seed, c.drift_rw_rad_per_sqrt_s))

    def ramp_voltage(self, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Piezo: Rampenspannung (V) und Richtung zur wahren Zeit t."""
        u, d = piezo_ramp(self.cfg, np.asarray(t, float) - self.session_start)
        return u * self.cfg.piezo_V_max, d

    def reference_times(self) -> np.ndarray:
        """Wahre Zeiten der Phasenreferenz-Marker (Beginn jeder Rampe bzw. Stufe)."""
        c = self.cfg
        if c.mode == "ramp" and c.ramp_Hz > 0:
            return self.t_ref + np.arange(0, self.T - self.REF_LEAD, 1.0 / c.ramp_Hz)
        if c.mode == "steps":
            return self.t_ref + np.arange(0, self.T - self.REF_LEAD, c.dwell_s)
        if c.mode == "piezo" and c.piezo_Hz > 0:        # Rampenbeginn (u = 0) im Lauf
            P = 1.0 / c.piezo_Hz
            ts0 = self.t0 - self.session_start
            k0 = np.ceil((ts0 + self.REF_LEAD) / P)
            k1 = np.floor((ts0 + self.T - self.REF_LEAD) / P)
            return self.session_start + np.arange(k0, k1 + 1) * P if k1 >= k0 else np.empty(0)
        return np.empty(0)

    def metadata(self) -> dict:
        c = self.cfg
        return dict(phase_mode=c.mode, phase_theta0_rad=float(c.theta0), phase_ramp_Hz=float(c.ramp_Hz),
                    phase_steps_rad=list(map(float, c.steps)), phase_dwell_s=float(c.dwell_s),
                    phase_marker_bit=int(c.marker_bit),
                    **(dict(phase_piezo_waveform=c.piezo_waveform, phase_piezo_Hz=float(c.piezo_Hz),
                            phase_piezo_V_max=float(c.piezo_V_max), phase_ramp_channel=c.ramp_channel,
                            phase_ramp_adc_bits=int(c.ramp_adc_bits), phase_ramp_adc_range_V=float(c.ramp_adc_range_V),
                            phase_ramp_lsb_V=float(c.ramp_adc_range_V / (2 ** (c.ramp_adc_bits - 1) - 1)),
                            phase_ref_alpha=float(c.ref_alpha))
                       if c.mode == "piezo" else {}))

    def ramp_raw(self, t: np.ndarray, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
        """Hilfs-ADC: Rohwert (LSB, int16) der Rampenspannung je Trigger und wahre Richtung."""
        c = self.cfg
        V, d = self.ramp_voltage(t)
        V = V + rng.normal(0.0, c.ramp_adc_noise_V, V.shape) if c.ramp_adc_noise_V > 0 else V
        full = 2 ** (c.ramp_adc_bits - 1) - 1
        raw = np.clip(np.round(V / c.ramp_adc_range_V * full), -full - 1, full)
        return raw.astype(np.int16 if c.ramp_adc_bits <= 16 else np.int32), d
