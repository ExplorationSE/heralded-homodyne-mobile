"""Marker-Zuordnung zwischen Harp (Sync-Eingang bzw. Marker-Eingang) und ADQ32 (SYNC) – Vorbericht 2, MARKER_PAARE."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import kernels as K


@dataclass
class MarkerMatch:
    tH: np.ndarray                  # gepaarte Harp-Markerzeiten (s)
    TD: np.ndarray                  # gepaarte Digitizer-Markerzeiten (s)
    kH: np.ndarray                  # Gitterindex (Harp-Zählung) der Paare
    shift: int
    uniqueness: float
    period_H: float
    period_D: float
    n_H: int
    n_D: int
    rejected_H: int
    rejected_D: int
    warnings: list = field(default_factory=list)

    @property
    def n_pairs(self) -> int:
        return len(self.tH)

    def info(self) -> dict:
        return dict(n_markers_H=self.n_H, n_markers_D=self.n_D, rejected_H=self.rejected_H, rejected_D=self.rejected_D,
                    n_pairs=self.n_pairs, shift=self.shift, uniqueness=self.uniqueness, period_H=self.period_H,
                    period_D=self.period_D)


def estimate_period(t: np.ndarray, nominal: float | None) -> float:
    """Markerperiode: Vorgabe oder häufigster Abstand (Auslassungen ergeben Vielfache)."""
    if nominal:
        return float(nominal)
    d = np.diff(np.sort(t))
    if len(d) == 0:
        return 1e-3
    return float(np.percentile(d, 25))


def match_markers(tH: np.ndarray, TD: np.ndarray, period: float | None = None, max_u: float = 0.5,
                  min_pairs: int = 10) -> MarkerMatch:
    tH = np.sort(np.asarray(tH, float)); TD = np.sort(np.asarray(TD, float))
    P = estimate_period(tH if len(tH) >= len(TD) else TD, period)
    gH = K.grid_index(tH, P)
    gD = K.grid_index(TD, P)
    xc = K.xcorr_shift(gH["k"], gD["k"])
    iH, iD = K.pair_by_index(gH["k"], gD["k"], xc["shift"])
    w = []
    if xc["uniqueness"] > max_u:
        w.append(f"Marker-Kreuzkorrelation nicht eindeutig (u = {xc['uniqueness']:.2f} > {max_u})")
    if len(iH) < min_pairs:
        w.append(f"nur {len(iH)} Markerpaare")
    return MarkerMatch(tH[gH["keep"][iH]], TD[gD["keep"][iD]], gH["k"][iH], xc["shift"], xc["uniqueness"],
                       gH["period"], gD["period"], len(tH), len(TD), gH["rejected"], gD["rejected"], w)
