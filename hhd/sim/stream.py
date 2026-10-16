"""Datenstrom-Modus: Abspielen eines simulierten Laufs in Zeitscheiben (Chunks), optional in Echtzeit.

Gedacht als Grundlage der simulierten Geräte (AP17) und des Servers (wie die Quellen in GX:
``open``/``start``/``read_chunk``/``stop``, Aufrufe aus genau einem Thread). Ein Chunk enthält

* die **rohen T2-Records** der HydraHarp 500 (uint32, wie aus dem Geräte-FIFO), dekodierbar mit
  ``hhd.io.ptu.T2Decoder`` über Chunkgrenzen hinweg;
* die in dieser Zeitscheibe **fertigen Records** des ADQ32 (Daten + Header-Felder) und die SYNC-Marker.

Die Daten sind bitgleich zu denen des Datei-Modus (gleicher Seed); das prüfen die Tests.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

import numpy as np

from .engine import BlockResult, RunResult


@dataclass
class Chunk:
    block_id: str
    dataset_type: str
    index: int
    t_start: float                  # s relativ zum Blockbeginn
    t_end: float
    harp_records: np.ndarray
    rec_data: np.ndarray
    rec_timestamp: np.ndarray
    rec_record_start: np.ndarray
    rec_trigger_source: np.ndarray
    rec_number: np.ndarray
    marker_timestamp: np.ndarray
    last_in_block: bool
    meta: dict = field(default_factory=dict)


class SimStream:
    def __init__(self, res: RunResult, chunk_s: float = 0.05, speed: float = 1.0, realtime: bool = True,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        if chunk_s <= 0 or speed <= 0:
            raise ValueError("chunk_s und speed > 0")
        self.res, self.chunk_s, self.speed, self.realtime = res, chunk_s, speed, realtime
        self._clock, self._sleep = clock, sleep

    def _block_chunks(self, br: BlockResult, t_wall0: float, t_offset: float) -> Iterator[Chunk]:
        tu = self.res.device_attrs["time_unit_s"]
        L = self.res.device_attrs["record_length"]
        fs = self.res.device_attrs["sampling_rate_Hz"]
        TD0 = float(self.res.clock(br.t0))
        dg = br.digitizer
        # Record ist verfügbar, wenn sein letztes Sample aufgenommen ist (Digitizer-Zeit → Blockzeit)
        rec_t = (dg.timestamp + dg.record_start) * tu + L / fs - TD0
        mk_t = dg.marker_ts * tu - TD0
        n = max(1, int(np.ceil(br.duration / self.chunk_s)))
        edges = np.minimum(np.arange(1, n + 1) * self.chunk_s, br.duration)
        edges[-1] = np.inf                      # Rest (Records am Blockende) in den letzten Chunk
        h_idx = np.searchsorted(br.harp_record_time_ps, edges[:-1] * 1e12, side="left")
        h_idx = np.append(h_idx, len(br.harp_records))
        r_idx = np.append(np.searchsorted(rec_t, edges[:-1], side="right"), len(rec_t))
        m_idx = np.append(np.searchsorted(mk_t, edges[:-1], side="right"), len(mk_t))
        h0 = r0 = m0 = 0
        for k in range(n):
            t1 = min((k + 1) * self.chunk_s, br.duration)
            if self.realtime:
                due = t_wall0 + (t_offset + t1) / self.speed
                wait = due - self._clock()
                if wait > 0:
                    self._sleep(wait)
            h1, r1, m1 = h_idx[k], r_idx[k], m_idx[k]
            yield Chunk(br.cfg.id, br.dataset_type, k, k * self.chunk_s, t1, br.harp_records[h0:h1],
                        dg.data[r0:r1], dg.timestamp[r0:r1], dg.record_start[r0:r1], dg.trigger_source[r0:r1],
                        np.arange(r0, r1, dtype=np.uint32), dg.marker_ts[m0:m1], k == n - 1,
                        meta=dict(attrs=br.attrs) if k == 0 else {})
            h0, r0, m0 = h1, r1, m1

    def __iter__(self) -> Iterator[Chunk]:
        t_wall0 = self._clock()
        t_off = 0.0
        for br in self.res.blocks:
            yield from self._block_chunks(br, t_wall0, t_off)
            t_off += br.duration


class StreamSource:
    """Geräteähnliche Hülle (Muster ``harp/sources/base.py`` in GX): open → start → read_chunk … → stop."""

    def __init__(self, res: RunResult, **kw):
        self._res, self._kw = res, kw
        self._it: Iterator[Chunk] | None = None
        self.running = False
        self.n_chunks = 0

    def open(self) -> dict:
        return dict(kind="sim", device=self._res.device_attrs, blocks=[b.cfg.id for b in self._res.blocks])

    def start(self) -> None:
        self._it = iter(SimStream(self._res, **self._kw))
        self.running = True

    def read_chunk(self) -> Chunk | None:
        if not self.running or self._it is None:
            return None
        try:
            c = next(self._it)
        except StopIteration:
            self.running = False
            return None
        self.n_chunks += 1
        return c

    def stop(self) -> None:
        self.running = False
        self._it = None

    def status(self) -> dict:
        return dict(running=self.running, chunks=self.n_chunks)
