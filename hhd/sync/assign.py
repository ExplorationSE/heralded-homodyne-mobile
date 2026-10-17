"""Zuordnung Segment (ADQ32-Record) ↔ Herald (Harp CH1) und Klassifikation – Vorbericht 2, ZUORDNUNG.

Segmentklassen (``SEG_*``): zugeordnet, verwaist (kein Herald im Fenster), mehrdeutig (≥ 2 Heralds; keine
Rateentscheidung), Zufalls- bzw. Zustandstrigger (über CH4 gekennzeichnet), periodisch (Lauf B, K-V: kein
Herald-Trigger).

Heraldklassen (``HER_*``): zugeordnet, sekundär (Mode liegt in einem fremden Segment, z. B. während der
Totzeit), ungetriggert. Für nicht zugeordnete Heralds zusätzlich der Grund (``REASON_*``): Digitizer belegt
(Totzeit: Record + Rearm), Rand (vor dem ersten möglichen Trigger bzw. nach dem letzten vollständigen Record),
sonst unerklärt.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import kernels as K
from .clockmap import ClockMap

SEG_MATCHED, SEG_ORPHAN, SEG_AMBIGUOUS, SEG_RANDOM, SEG_STATE, SEG_PERIODIC = 0, 1, 2, 3, 4, 5
SEG_NAMES = {SEG_MATCHED: "zugeordnet", SEG_ORPHAN: "verwaist", SEG_AMBIGUOUS: "mehrdeutig",
             SEG_RANDOM: "zufallstrigger", SEG_STATE: "zustandstrigger", SEG_PERIODIC: "periodisch"}
HER_MATCHED, HER_SECONDARY, HER_UNTRIGGERED = 0, 1, 2
HER_NAMES = {HER_MATCHED: "zugeordnet", HER_SECONDARY: "sekundär", HER_UNTRIGGERED: "ungetriggert"}
REASON_NONE, REASON_BUSY, REASON_EDGE, REASON_OTHER = 0, 1, 2, 3
REASON_NAMES = {REASON_NONE: "-", REASON_BUSY: "Totzeit", REASON_EDGE: "Rand", REASON_OTHER: "unerklärt"}


@dataclass
class Assignment:
    hD: np.ndarray                   # Heralds in Digitizer-Zeit (s)
    seg_class: np.ndarray            # int8 je Record
    seg_herald: np.ndarray           # Herald-Index je Record (−1)
    seg_n_secondary: np.ndarray      # fremde Heralds, deren Mode im Record liegt
    seg_overlap_prev: np.ndarray     # Samplefenster überlappt den vorigen Record
    herald_class: np.ndarray         # int8 je Herald
    herald_reason: np.ndarray        # int8 je Herald
    herald_segment: np.ndarray       # Record (zugeordnet bzw. sekundär) oder −1
    dHT: float                       # Trigger − abgebildeter Herald (s, Digitizer-Zeit)
    sigma_HT: float
    tol: float
    n_candidates: np.ndarray = field(default_factory=lambda: np.empty(0, np.int64))
    tag_offset: float = float("nan")

    def counts(self) -> dict:
        seg = {SEG_NAMES[k]: int(np.sum(self.seg_class == k)) for k in SEG_NAMES}
        her = {HER_NAMES[k]: int(np.sum(self.herald_class == k)) for k in HER_NAMES}
        rea = {REASON_NAMES[k]: int(np.sum((self.herald_class != HER_MATCHED) & (self.herald_reason == k)))
               for k in (REASON_BUSY, REASON_EDGE, REASON_OTHER)}
        return dict(segments=seg, heralds=her, reasons=rea,
                    segments_with_secondary=int(np.sum(self.seg_n_secondary > 0)),
                    segments_overlapping=int(np.sum(self.seg_overlap_prev)))


def trigger_offset(trig: np.ndarray, ref: np.ndarray, floor: float = 0.0) -> tuple[float, float, np.ndarray]:
    """Robuste Pfaddifferenz trig − ref (nächster Nachbar) und ihre Streuung."""
    if len(ref) == 0 or len(trig) == 0:
        return float("nan"), float("nan"), np.zeros(len(trig), bool)
    _, d = K.nearest_diff(trig, ref)
    c, s, use = K.robust_center(d, k=8.0, floor=floor)
    sig = float(np.std(d[use])) if use.sum() > 2 else s
    return c, sig, use


def assign_heralds(tH: np.ndarray, cmap: ClockMap, trig: np.ndarray, first: np.ndarray, last: np.ndarray,
                   run_D: tuple[float, float], rearm: float, pretrig_s: float, post_s: float,
                   tol_min: float = 2e-9, k_sigma: float = 6.0, dHT: float | None = None,
                   mode_offset: float | None = None, tags: np.ndarray | None = None,
                   tag_class: int = SEG_RANDOM) -> Assignment:
    """``tH``: Herald-Zeiten (Harp, s, sortiert); ``trig``, ``first``, ``last``: Trigger, erstes und letztes Sample
    je Record (Digitizer-Zeit, s); ``mode_offset``: Moden­zentrum − abgebildeter Herald (Δ), falls bekannt, sonst
    wird die Triggerzeit als Ort genommen; ``tags``: Zufalls-/Zustandstrigger (Harp, s) für verwaiste Records."""
    hD = cmap.D(tH)
    n_seg, n_h = len(trig), len(hD)
    if dHT is None:
        dHT, sig, _ = trigger_offset(trig, hD)
    else:
        _, sig, _ = trigger_offset(trig - dHT, hD, floor=tol_min)
        sig = sig if np.isfinite(sig) else 0.0
    if not np.isfinite(dHT):
        dHT, sig = 0.0, 0.0
    tol = max(k_sigma * sig, tol_min)
    exp_trig = hD + dHT
    ncand, first_i = K.count_in_window(exp_trig, trig - tol, trig + tol)
    seg_class = np.where(ncand == 1, SEG_MATCHED, np.where(ncand == 0, SEG_ORPHAN, SEG_AMBIGUOUS)).astype(np.int8)
    seg_herald = np.where(ncand == 1, first_i, -1).astype(np.int64)
    # Zufalls-/Zustandstrigger (CH4) für verwaiste Records
    tag_off = tag_segments(cmap, trig, seg_class, seg_class == SEG_ORPHAN, tags, tag_class, tol_min, k_sigma)
    herald_class = np.full(n_h, HER_UNTRIGGERED, np.int8)
    herald_segment = -np.ones(n_h, np.int64)
    m = seg_herald >= 0
    herald_class[seg_herald[m]] = HER_MATCHED
    herald_segment[seg_herald[m]] = np.flatnonzero(m)
    # Modenort der Heralds; fremde Moden im Record (sekundär), Überlappung der Samplefenster
    pos = hD + (dHT if mode_offset is None else mode_offset)
    win = K.window_index(pos, first, last)
    sec = (herald_class != HER_MATCHED) & (win >= 0)
    herald_class[sec] = HER_SECONDARY
    herald_segment[sec] = win[sec]
    cnt, _ = K.count_in_window(np.sort(pos), first, last + 1e-15)
    own = (seg_herald >= 0) & (np.abs(pos[np.clip(seg_herald, 0, None)] - (first + last) / 2) <= (last - first) / 2 + 1e-15)
    seg_n_sec = (cnt - own.astype(np.int64)).astype(np.int32)
    seg_overlap_prev = np.zeros(n_seg, bool)
    if n_seg > 1:
        seg_overlap_prev[1:] = first[1:] < last[:-1]
    # Gründe für fehlende eigene Records
    reason = np.full(n_h, REASON_NONE, np.int8)
    un = herald_class != HER_MATCHED
    if un.any():
        busy_w = K.window_index(exp_trig, trig, last + rearm + tol)     # Grenzfall innerhalb der Toleranz
        lo_ok, hi_ok = run_D[0] + pretrig_s, run_D[1] - post_s
        edge = (exp_trig < lo_ok) | (exp_trig > hi_ok)
        reason[un] = REASON_OTHER
        reason[un & edge] = REASON_EDGE
        reason[un & ~edge & (busy_w >= 0)] = REASON_BUSY
    return Assignment(hD, seg_class, seg_herald, seg_n_sec, seg_overlap_prev, herald_class, reason, herald_segment,
                      float(dHT), float(sig), float(tol), ncand, tag_off)


def tag_segments(cmap: ClockMap, trig: np.ndarray, seg_class: np.ndarray, mask: np.ndarray, tags: np.ndarray | None,
                 tag_class: int, tol_min: float = 2e-9, k_sigma: float = 6.0) -> float:
    """Records mit genau einer CH4-Kennung im Toleranzfenster → ``tag_class`` (in-place). Rückgabe: Pfadversatz."""
    if tags is None or len(tags) == 0 or not mask.any():
        return float("nan")
    tD = np.sort(cmap.D(tags))
    off, tsig, _ = trigger_offset(trig[mask], tD)
    ttol = max(k_sigma * tsig, tol_min)
    nt, _ = K.count_in_window(tD + off, trig - ttol, trig + ttol)
    seg_class[mask & (nt == 1)] = tag_class
    return off


def classify_periodic(n_seg: int) -> np.ndarray:
    return np.full(n_seg, SEG_PERIODIC, np.int8)
