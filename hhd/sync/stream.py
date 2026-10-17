"""Chunkweiser Synchronisierer (Datenstrom-Modus, begrenzter Speicher) – Vorstufe des Rust-Kerns (AP9).

Ablauf je Lauf:

1. *Einrasten*: Marker beider Ströme puffern, bis je ``n_lock`` vorliegen; dann einmal die Batch-Zuordnung
   (Gitterindex + Kreuzkorrelation über das Auslassmuster) → Abbildung.
2. *Nachführen*: jeder neue Digitizer-Marker wird über die aktuelle Abbildung dem nächsten Harp-Marker
   zugeordnet (Fenster ``max(10·σ_r, 1 µs)``); robuste Gerade über die letzten ``window`` Paare.
3. *Zuordnen mit Latenz*: Records gelten als final, sobald ihr Trigger vor dem jüngsten gepaarten Marker liegt;
   Heralds werden nach Zuordnung verworfen (Speicher O(Latenz·Rate)).

Ergebnis: Segmentklasse und Herald (laufender Index im Harp-Strom) je Record; Stückweise-Korrektur und
Δ-Kalibrierung bleiben dem Batch-Lauf vorbehalten.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import assign as A
from . import kernels as K
from .clockmap import ClockMap
from .markers import match_markers


@dataclass
class StreamState:
    locked: bool = False
    cmap: ClockMap | None = None
    dHT: float | None = None
    sigma_HT: float = 0.0
    n_pairs: int = 0
    n_refits: int = 0
    max_buffer: int = 0


class StreamingSynchronizer:
    def __init__(self, marker_period: float | None = None, n_lock: int = 64, window: int = 2000,
                 tol_min: float = 2e-9, k_sigma: float = 6.0):
        self.P = marker_period
        self.n_lock, self.window = n_lock, window
        self.tol_min, self.k_sigma = tol_min, k_sigma
        self.st = StreamState()
        self._Hm = np.empty(0); self._Dm = np.empty(0)          # ungepaarte Marker
        self._pH = np.empty(0); self._pD = np.empty(0)          # gepaarte Marker (Fenster)
        self._her = np.empty(0); self._her0 = 0                 # Herald-Puffer, globaler Index des ersten
        self._trig = np.empty(0); self._trig0 = 0               # offene Trigger
        self.seg_class: list[np.ndarray] = []
        self.seg_herald: list[np.ndarray] = []

    # --------------------------------------------------------------------------- Eingang
    def feed_harp(self, markers_s: np.ndarray, heralds_s: np.ndarray) -> None:
        self._Hm = np.concatenate([self._Hm, np.asarray(markers_s, float)])
        self._her = np.concatenate([self._her, np.asarray(heralds_s, float)])
        self._step()

    def feed_digitizer(self, markers_D: np.ndarray, trig_D: np.ndarray) -> None:
        self._Dm = np.concatenate([self._Dm, np.asarray(markers_D, float)])
        self._trig = np.concatenate([self._trig, np.asarray(trig_D, float)])
        self._step()

    # --------------------------------------------------------------------------- intern
    def _refit(self) -> None:
        x, y = self._pD[-self.window:], self._pH[-self.window:]
        lin = K.robust_linear(x, y, floor=1e-12)
        self.st.cmap = ClockMap(lin["a0"], lin["b"], lin["x0"], lin["cov"], lin["sigma_r"], int(lin["used"].sum()))
        self.st.n_refits += 1

    def _lock(self) -> None:
        mm = match_markers(self._Hm, self._Dm, self.P)
        if mm.n_pairs < self.n_lock // 2 or mm.uniqueness > 0.5:
            return
        self.P = mm.period_H
        self._pH, self._pD = mm.tH, mm.TD
        last_H = mm.tH[-1]; last_D = mm.TD[-1]
        self._Hm = self._Hm[self._Hm > last_H]
        self._Dm = self._Dm[self._Dm > last_D]
        self.st.locked = True
        self._refit()

    def _track(self) -> None:
        cm = self.st.cmap
        if len(self._Dm) == 0 or len(self._Hm) == 0:
            return
        pred = cm.H(self._Dm)
        j, d = K.nearest_diff(pred, np.sort(self._Hm))
        tol = max(10 * cm.sigma_r, 1e-6)
        ok = np.abs(d) < tol
        # nur Digitizer-Marker, deren Harp-Gegenstück schon da sein müsste
        horizon = self._Hm.max() - tol
        done = pred < horizon
        Hs = np.sort(self._Hm)
        if ok.any():
            self._pH = np.concatenate([self._pH, Hs[j[ok & done]]])[-4 * self.window:]
            self._pD = np.concatenate([self._pD, self._Dm[ok & done]])[-4 * self.window:]
            o = np.argsort(self._pD); self._pD, self._pH = self._pD[o], self._pH[o]
            self._refit()
        self._Dm = self._Dm[~done]
        if len(self._pH):
            self._Hm = self._Hm[self._Hm > self._pH[-1] - tol]

    def _assign(self, final: bool = False) -> None:
        if len(self._trig) == 0:
            return
        cm = self.st.cmap
        lim = np.inf if final else (self._pD[-1] if len(self._pD) else -np.inf)
        n = int(np.searchsorted(self._trig, lim, side="right"))
        if n == 0:
            return
        trig = self._trig[:n]
        hD = cm.D(self._her)
        if self.st.dHT is None:
            if n < 20 and not final:
                return
            self.st.dHT, self.st.sigma_HT, _ = A.trigger_offset(trig, hD)
        tol = np.maximum(self.k_sigma * np.hypot(self.st.sigma_HT, cm.sigma_H(trig)), self.tol_min)
        nc, first = K.count_in_window(hD + self.st.dHT, trig - tol, trig + tol)
        if n >= 5 and np.mean(nc == 1) < 0.5:
            # Abbildung ist mit dem Refit gesprungen: Pfaddifferenz aus diesem Block neu schätzen
            d0, s0, _ = A.trigger_offset(trig, hD)
            if np.isfinite(d0):
                nc2, f2 = K.count_in_window(hD + d0, trig - tol, trig + tol)
                if np.mean(nc2 == 1) > np.mean(nc == 1):
                    nc, first, self.st.dHT = nc2, f2, d0
        cls = np.where(nc == 1, A.SEG_MATCHED, np.where(nc == 0, A.SEG_ORPHAN, A.SEG_AMBIGUOUS)).astype(np.int8)
        # lokale Nachführung: Pfaddifferenz der Nachbarn (Irrfahrt zwischen den Refits) für nicht eindeutige Trigger
        m = nc == 1
        if m.sum() >= 5 and (~m).any():
            d_loc = np.interp(trig, trig[m], trig[m] - hD[first[m]])
            ex = np.sort(hD)
            lo_i = np.searchsorted(ex, trig - d_loc - tol); hi_i = np.searchsorted(ex, trig - d_loc + tol, side="right")
            fix = ~m & (hi_i - lo_i == 1)
            nc = np.where(fix, 1, nc); first = np.where(fix, lo_i, first)
            m = nc == 1
        if m.sum() >= 5:
            d = trig[m] - hD[first[m]]
            c, sg, use = K.robust_center(d, k=6.0, floor=1e-12)
            if np.isfinite(c):
                self.st.dHT = c
                self.st.sigma_HT = float(np.std(d[use])) if use.sum() > 2 else self.st.sigma_HT
        self.seg_class.append(cls)
        self.seg_herald.append(np.where(nc == 1, first + self._her0, -1))
        self._trig = self._trig[n:]
        # Heralds vor dem ältesten offenen Trigger (minus Sicherheitsabstand) verwerfen
        keep_from = trig[-1] - 10 * float(np.max(tol)) - 1e-6
        drop = int(np.searchsorted(hD + self.st.dHT, keep_from))
        self._her = self._her[drop:]; self._her0 += drop

    def _step(self) -> None:
        if not self.st.locked:
            if len(self._Hm) >= self.n_lock and len(self._Dm) >= self.n_lock:
                self._lock()
            if not self.st.locked:
                self.st.max_buffer = max(self.st.max_buffer, len(self._her) + len(self._trig))
                return
        self._track()
        self._assign()
        self.st.max_buffer = max(self.st.max_buffer, len(self._her) + len(self._trig))

    def finalize(self) -> dict:
        if not self.st.locked:
            self._lock()
        if self.st.locked:
            self._assign(final=True)
        sc = np.concatenate(self.seg_class) if self.seg_class else np.empty(0, np.int8)
        sh = np.concatenate(self.seg_herald) if self.seg_herald else np.empty(0, np.int64)
        return dict(seg_class=sc, seg_herald=sh, locked=self.st.locked, dHT=self.st.dHT, n_refits=self.st.n_refits,
                    max_buffer=self.st.max_buffer, cmap=self.st.cmap.to_dict() if self.st.cmap else None)


def stream_run(run, chunk_records: int = 1024) -> dict:
    """Spielt einen Lauf chunkweise ein: Harp-Chunks aus der PTU-Datei, Digitizer anteilig in Zeitordnung."""
    n_ptu = max(int(run.ptu_tags.get("TTResult_NumberOfRecords", 0)), 1)
    s = StreamingSynchronizer()
    TDm = run.markers_D(); trig = run.trigger_D()
    seen = 0
    iD = iT = 0
    for hs in run.iter_harp(chunk_records):
        s.feed_harp(hs.role_s("sync_marker"), hs.role_s("herald"))
        seen += hs.n_records
        f = min(seen / n_ptu, 1.0)
        jD, jT = int(f * len(TDm)), int(f * len(trig))
        s.feed_digitizer(TDm[iD:jD], trig[iT:jT])
        iD, iT = jD, jT
    s.feed_digitizer(TDm[iD:], trig[iT:])
    return s.finalize()
