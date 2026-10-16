"""Gerätemodell HydraHarp 500 im T2-Modus (Strom 1).

Herstellerangaben **[Spez., HydraHarp 500 Manual, Kap. 6.3 und 9.3.1]**:

* T2-Auflösung 1 ps; Einzelmessungs-Zeitunsicherheit typ. 2,5 ps rms;
* Totzeit je Kanal < 680 ps (ETR) bzw. < 6,8 ns (CFD), keine Totzeit über Kanäle hinweg;
* 4–8 (Modell S) bzw. 8–16 (Modell M) Eingänge; einstellbarer Eingangs-Offset ±100 ns;
* 4 externe Marker-Eingänge (LVTTL), Puls und Pause je > 50 ns, Marker-Zeitfehler in T2 < 100 ns,
  programmierbarer Marker-Hold-off;
* dauerhafter Durchsatz über USB typ. 85·10⁶ Ereignisse/s (abhängig vom PC).

Modellannahmen: Zeitfehler gaußförmig; Totzeit nicht verlängernd; die Harp-Zeitbasis ist die wahre Zeitachse
(Referenz), jede Messung (Block) beginnt bei 0 ps.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..io.ptu import KIND_EVENT, KIND_MARKER
from .config import SimConfig

MAX_SUSTAINED_RATE = 85e6


def apply_dead_time(t: np.ndarray, dead: float) -> np.ndarray:
    """Maske der akzeptierten Ereignisse (sortierte Zeiten, nicht verlängernde Totzeit)."""
    keep = np.ones(len(t), bool)
    if len(t) < 2:
        return keep
    close = np.flatnonzero(np.diff(t) < dead) + 1
    if len(close) == 0:
        return keep
    last = -np.inf
    # nur die Umgebung kritischer Ereignisse sequentiell prüfen
    for i in range(len(t)):
        if t[i] - last < dead:
            keep[i] = False
        else:
            last = t[i]
    return keep


def holdoff(t: np.ndarray, hold: float) -> np.ndarray:
    return apply_dead_time(t, hold) if hold > 0 else np.ones(len(t), bool)


@dataclass
class HarpRecording:
    times_ps: np.ndarray
    kinds: np.ndarray
    codes: np.ndarray
    per_input_ps: dict          # {Kanal: ps je Eingangspuls, −1 = verloren} in Eingabereihenfolge
    per_marker_ps: dict         # {Bit: ps je Markerpuls, −1 = verloren}
    spurious_ps: np.ndarray
    warnings: list


class HydraHarp500:
    def __init__(self, cfg: SimConfig):
        self.cfg = cfg
        self.h = cfg.harp

    def record(self, rng: np.random.Generator, duration: float, inputs: dict[int, np.ndarray],
               markers: dict[int, np.ndarray]) -> HarpRecording:
        """``inputs``: {Kanal: Ankunftszeiten am Eingang in s, relativ zum Messbeginn};
        ``markers``: {Bit: Zeiten der Marker-Flanken in s}."""
        h = self.h
        T_ps = int(round(duration / h.resolution))
        times, kinds, codes = [], [], []
        per_in, per_mk = {}, {}
        warn = []
        total = 0
        for ch, t in inputs.items():
            t = np.asarray(t, float)
            tj = t + rng.normal(0, h.jitter_rms, len(t)) + float(h.channel_offsets.get(ch, h.channel_offsets.get(str(ch), 0.0)))
            ps = np.floor(tj / h.resolution).astype(np.int64)
            o = np.argsort(ps, kind="stable")
            ok = np.zeros(len(t), bool)
            okk = apply_dead_time(ps[o] * h.resolution, self.cfg.harp_dead)
            ok[o] = okk
            ok &= (ps >= 0) & (ps < T_ps)
            per_in[ch] = np.where(ok, ps, -1)
            times.append(ps[ok]); kinds.append(np.full(ok.sum(), KIND_EVENT)); codes.append(np.full(ok.sum(), ch))
            total += ok.sum()
        for bit, t in markers.items():
            t = np.asarray(t, float)
            err = h.marker_offset + rng.normal(0, h.marker_error_rms, len(t))
            err = np.clip(err, -h.marker_error_max, h.marker_error_max)
            ps = np.floor((t + err) / h.resolution).astype(np.int64)
            o = np.argsort(ps, kind="stable")
            ok = np.zeros(len(t), bool)
            ok[o] = holdoff(ps[o] * h.resolution, h.marker_holdoff)
            ok &= (ps >= 0) & (ps < T_ps)
            per_mk[bit] = np.where(ok, ps, -1)
            times.append(ps[ok]); kinds.append(np.full(ok.sum(), KIND_MARKER)); codes.append(np.full(ok.sum(), bit))
            total += ok.sum()
        n_sp = rng.poisson(h.R_spurious * duration)
        sp = np.sort(rng.integers(0, max(T_ps, 1), n_sp)).astype(np.int64)
        times.append(sp); kinds.append(np.full(n_sp, KIND_MARKER)); codes.append(np.full(n_sp, h.marker_bit))
        if duration > 0 and total / duration > MAX_SUSTAINED_RATE:
            warn.append("Ereignisrate über 85 Mcps (dauerhafter USB-Durchsatz der HydraHarp 500)")
        return HarpRecording(np.concatenate(times), np.concatenate(kinds), np.concatenate(codes), per_in, per_mk, sp, warn)
