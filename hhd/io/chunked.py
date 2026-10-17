"""Zeitfenster-Leser für lange Läufe (begrenzter Speicher): PTU-Records und Digitizer-Köpfe in Zeitfenstern.

Beide Leser lesen die Dateien blockweise und halten nur den laufenden Block plus einen Rest bis zur nächsten
Fenstergrenze. Der Speicher hängt von ``chunk``/``window`` und den Raten ab, nicht von der Laufdauer.
Fenster k umfasst [t0 + k·w, t0 + (k+1)·w); alles vor t0 fällt in Fenster 0.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import h5py
import numpy as np

from . import ptu as P


def _record_times(rec: np.ndarray, ofl0: int) -> tuple[np.ndarray, int]:
    """Zeit (ps) jedes T2-Records einschließlich Überlauf-/Sonderrecords; Rückgabe (Zeiten, neuer Überlaufstand)."""
    r = rec.astype(np.int64)
    is_ofl = ((r >> 31) == 1) & (((r >> 25) & 0x3F) == 63)
    tt = r & 0x1FFFFFF
    cum = ofl0 + np.cumsum(np.where(is_ofl, np.where(tt == 0, 1, tt), 0))
    t = np.where(is_ofl, cum * P.T2_WRAP, cum * P.T2_WRAP + tt)
    return t, int(cum[-1]) if len(cum) else ofl0


def first_sync_ps(path, chunk_records: int = 1 << 16, max_blocks: int = 64) -> int | None:
    """Zeit des ersten Sync-/Timing-Ereignisses auf Kanal 0 (Sync-Eingang), ohne die Datei ganz zu lesen."""
    dec = P.T2Decoder()
    for i, rec in enumerate(P.iter_ptu_records(path, chunk_records)):
        ev = dec.decode(rec)
        m = ev.event_channel == 0
        if m.any():
            return int(ev.event_time_ps[m][0])
        if i >= max_blocks:
            break
    return None


def iter_ptu_windows(path, t0_ps: int, window_ps: int, chunk_records: int = 1 << 16) -> Iterator[tuple[int, np.ndarray]]:
    """(Fensterindex, rohe Records) in Zeitordnung; leere Fenster werden mit leerem Array ausgegeben."""
    ofl = 0
    pend = np.empty(0, np.uint32); pend_t = np.empty(0, np.int64)
    k = 0
    for rec in P.iter_ptu_records(path, chunk_records):
        t, ofl = _record_times(rec, ofl)
        pend = np.concatenate([pend, rec]); pend_t = np.concatenate([pend_t, t])
        while len(pend_t) and pend_t[-1] >= t0_ps + (k + 1) * window_ps:
            j = int(np.searchsorted(pend_t, t0_ps + (k + 1) * window_ps, side="left"))
            yield k, pend[:j]
            pend, pend_t = pend[j:], pend_t[j:]
            k += 1
    yield k, pend


@dataclass
class DigWindow:
    index: np.ndarray            # globale Record-Indizes des Fensters
    timestamp: np.ndarray        # time_unit
    record_start: np.ndarray
    trigger_source: np.ndarray


def iter_record_windows(h5_path, block: str, t0_tu: int, window_tu: int, chunk: int = 1 << 15) -> Iterator[tuple[int, DigWindow]]:
    """Digitizer-Köpfe eines Blocks in Zeitfenstern (Zeitstempel in time_unit, je Lauf aufsteigend)."""
    with h5py.File(h5_path, "r") as f:
        rec = f["blocks"][block]["records"]
        n = rec["timestamp"].shape[0]
        k = 0
        pend = None
        for i0 in range(0, n, chunk):
            ts = rec["timestamp"][i0:i0 + chunk].astype(np.int64)
            blk = DigWindow(np.arange(i0, i0 + len(ts), dtype=np.int64), ts,
                            rec["record_start"][i0:i0 + chunk].astype(np.int64), rec["trigger_source"][i0:i0 + chunk])
            pend = blk if pend is None else DigWindow(*(np.concatenate([getattr(pend, a), getattr(blk, a)])
                                                        for a in ("index", "timestamp", "record_start", "trigger_source")))
            while len(pend.timestamp) and pend.timestamp[-1] >= t0_tu + (k + 1) * window_tu:
                j = int(np.searchsorted(pend.timestamp, t0_tu + (k + 1) * window_tu, side="left"))
                yield k, DigWindow(pend.index[:j], pend.timestamp[:j], pend.record_start[:j], pend.trigger_source[:j])
                pend = DigWindow(pend.index[j:], pend.timestamp[j:], pend.record_start[j:], pend.trigger_source[j:])
                k += 1
        if pend is None:
            pend = DigWindow(np.empty(0, np.int64), np.empty(0, np.int64), np.empty(0, np.int64), np.empty(0, np.uint8))
        yield k, pend


def iter_marker_windows(h5_path, block: str, t0_tu: int, window_tu: int, chunk: int = 1 << 16,
                        with_b: bool = False) -> Iterator[tuple[int, np.ndarray, np.ndarray | None]]:
    """SYNC-Marker (time_unit) eines Blocks in Zeitfenstern, optional mit ``markers/data_b`` (live, Kanal B an SYNC).
    Nicht aufsteigend gespeicherte Marker werden einmal ganz gelesen und sortiert (Rückfall)."""
    with h5py.File(h5_path, "r") as f:
        g = f["blocks"][block]["markers"]
        ds = g["timestamp"]
        db = g["data_b"] if with_b and "data_b" in g else None
        n = ds.shape[0]
        mono = True
        last = None
        for i0 in range(0, n, chunk):
            x = ds[i0:i0 + chunk].astype(np.int64)
            if len(x) and ((last is not None and x[0] < last) or np.any(np.diff(x) < 0)):
                mono = False; break
            if len(x):
                last = int(x[-1])

        def blocks():
            if mono:
                for i0 in range(0, n, chunk):
                    yield ds[i0:i0 + chunk].astype(np.int64), (db[i0:i0 + chunk] if db is not None else None)
            else:
                allx = ds[:].astype(np.int64); o = np.argsort(allx, kind="stable")
                yield allx[o], (db[:][o] if db is not None else None)

        k = 0
        pend = np.empty(0, np.int64); pb = None
        for x, b in blocks():
            pend = np.concatenate([pend, x])
            if b is not None:
                pb = b if pb is None else np.concatenate([pb, b])
            while len(pend) and pend[-1] >= t0_tu + (k + 1) * window_tu:
                j = int(np.searchsorted(pend, t0_tu + (k + 1) * window_tu, side="left"))
                yield k, pend[:j], (pb[:j] if pb is not None else None)
                pend = pend[j:]; pb = pb[j:] if pb is not None else None
                k += 1
        yield k, pend, pb

