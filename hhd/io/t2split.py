"""Zerlegen der HydraHarp-T2-Records in einem eigenen Prozess (ab 0.15, Live-Pfad).

Der Messablauf (Aufnahme → Schreiben der PTU-Datei) darf durch das Dekodieren nie aufgehalten werden. Deshalb:

* :class:`T2SplitProcess` startet einen eigenen Prozess (``multiprocessing`` spawn, Windows-sicher; Ziel
  :func:`_worker` liegt in diesem importierbaren Modul). Daten laufen über einen **begrenzten Ringpuffer im Shared
  Memory** (``n_slots`` Slots, Eingang T2-Records, Ausgang Zeiten); über die Queues gehen nur kurze Nachrichten.
* :meth:`submit` blockiert nie: Ist kein Slot frei, bleibt der Block im **Rückstau** des Hauptprozesses (nichts wird
  verworfen). Übersteigt der Rückstau ``max_backlog_mb``, meldet ``submit`` :class:`SplitOverload` – der Rechner
  kommt mit der Ereignisrate nicht mit, der Lauf muss angehalten werden (Ratenbegrenzung, ``hhd.server.rateguard``).
* :meth:`poll` holt fertige Ergebnisse ohne zu warten; :meth:`finish_run` wartet am Laufende auf den Rest.
* Stirbt der Prozess oder meldet er einen Fehler, lösen ``submit``/``poll``/``finish_run`` :class:`SplitProcessError`
  aus (laut, kein stiller Rückfall auf seriell).

Ergebnis je Block wie bisher seriell in ``HydraHarp500Live.read``: Herald- und Markerzeiten (s) nach Kanalrollen
(``split_events``) und das Zeitende (ps) des Blocks; :func:`split_serial` ist die serielle Referenz.
"""
from __future__ import annotations

import os
import queue
import time
import traceback
from collections import deque
from dataclasses import dataclass

import numpy as np

from .ptu import T2_WRAP, T2Decoder


class SplitProcessError(RuntimeError):
    """Zerlege-Prozess beendet oder fehlerhaft."""


class SplitOverload(RuntimeError):
    """Rückstau vor dem Zerlege-Prozess über der Grenze: Ereignisrate zu hoch für diesen Rechner."""


@dataclass
class SplitResult:
    seq: int
    herald_s: np.ndarray
    markers_s: np.ndarray
    last_ps: int
    n_records: int


def _split_block(dec: T2Decoder, roles, rec: np.ndarray):
    from .session import split_events
    ev = dec.decode(rec)
    hs = split_events(ev, *roles)
    last = max(int(ev.event_time_ps[-1]) if len(ev.event_time_ps) else 0, dec.ofl * T2_WRAP)
    return hs.role_s("herald"), hs.role_s("sync_marker"), last


def split_serial(blocks, roles) -> list[SplitResult]:
    """Serielle Referenz (wie bis 0.15 im Messablauf)."""
    dec = T2Decoder()
    out = []
    for i, rec in enumerate(blocks):
        h, m, last = _split_block(dec, roles, rec)
        out.append(SplitResult(i, h, m, last, len(rec)))
    return out


def _attach(name: str):
    from multiprocessing import shared_memory
    try:
        return shared_memory.SharedMemory(name=name, track=False)     # Python ≥ 3.13
    except TypeError:
        return shared_memory.SharedMemory(name=name)


def _worker(q_in, q_out, in_name: str, out_name: str, slot_rec: int) -> None:
    """Prozessziel. Nachrichten: ("run", roles) setzt den Dekoder zurück, ("data", seq, slot, n) zerlegt Slot ``slot``
    (n Records) und schreibt Herald-/Markerzeiten in denselben Ausgabe-Slot, ("end", tag) quittiert, None beendet.
    Fehler gehen als ("error", text) zurück."""
    for v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(v, "1")
    dec, roles = T2Decoder(), ({0: "sync_marker", 1: "herald"}, {})
    shm_in = shm_out = None
    try:
        shm_in, shm_out = _attach(in_name), _attach(out_name)
        n_slots = shm_in.size // (4 * slot_rec)
        IN = np.ndarray((n_slots, slot_rec), np.uint32, shm_in.buf)
        OUT = np.ndarray((n_slots, slot_rec), np.float64, shm_out.buf)
        crash = os.environ.get("HHD_T2SPLIT_CRASH_AT", "")
        while True:
            it = q_in.get()
            if it is None:
                break
            kind = it[0]
            if kind == "run":
                dec, roles = T2Decoder(), it[1]
            elif kind == "data":
                _, seq, slot, n = it
                if crash == str(seq):                                    # Tests: Absturz nachstellen
                    os._exit(3)
                h, m, last = _split_block(dec, roles, IN[slot, :n])
                if len(h) + len(m) > slot_rec:
                    raise RuntimeError("Ausgabe-Slot zu klein")
                OUT[slot, :len(h)] = h
                OUT[slot, len(h):len(h) + len(m)] = m
                q_out.put(("res", seq, slot, len(h), len(m), last, n))
            elif kind == "end":
                q_out.put(("end", it[1]))
            elif kind == "ping":
                q_out.put(("pong", it[1]))
    except Exception:  # noqa: BLE001 – an den Messablauf weiterreichen
        q_out.put(("error", traceback.format_exc()))
    finally:
        IN = OUT = None
        for m in (shm_in, shm_out):
            if m is not None:
                try:
                    m.close()
                except Exception:  # noqa: BLE001
                    pass


class T2SplitProcess:
    """Zerlege-Prozess mit begrenztem Ringpuffer im Shared Memory und Rückstau im Hauptprozess (siehe Modultext).

    ``n_slots`` Slots zu je ``slot_mrec`` Mio. Records (Eingang uint32, Ausgang float64): Standard 16 × 1 Mio. →
    64 MB + 128 MB (1 Mio. Records: Dekodieren im Cache am schnellsten, gemessen). Größere Blöcke werden auf mehrere Slots aufgeteilt (die Dekodierung ist fortlaufend)."""

    def __init__(self, n_slots: int = 16, slot_mrec: float = 1.0, max_backlog_mb: float = 512.0,
                 start_timeout_s: float = 60.0):
        import multiprocessing as mp
        from multiprocessing import shared_memory
        ctx = mp.get_context("spawn")
        self.n_slots, self.slot_rec = int(n_slots), int(slot_mrec * 1e6)
        self.max_backlog = float(max_backlog_mb) * 1e6
        self._shm_in = shared_memory.SharedMemory(create=True, size=4 * self.n_slots * self.slot_rec)
        self._shm_out = shared_memory.SharedMemory(create=True, size=8 * self.n_slots * self.slot_rec)
        self._IN = np.ndarray((self.n_slots, self.slot_rec), np.uint32, self._shm_in.buf)
        self._OUT = np.ndarray((self.n_slots, self.slot_rec), np.float64, self._shm_out.buf)
        self._free = deque(range(self.n_slots))
        self._in = ctx.Queue()
        self._out = ctx.Queue()
        self.proc = ctx.Process(target=_worker, args=(self._in, self._out, self._shm_in.name, self._shm_out.name,
                                                       self.slot_rec), daemon=True, name="hhd-t2split")
        self.proc.start()
        self._backlog: deque = deque()          # ("run", roles) | ("data", seq, rec) | ("end", tag)
        self.backlog_bytes = 0
        self.max_backlog_seen = 0
        self.seq = 0
        self.n_done = 0
        self._ended: set = set()
        self._ready: list[SplitResult] = []
        self.run_seq0 = 0                       # erste Blocknummer des laufenden Laufs (ältere: abgebrochener Lauf)
        self.error = ""
        self._closed = False
        self._ping(start_timeout_s)

    # ------------------------------------------------------------------ intern
    def _check_alive(self) -> None:
        if self.error:
            raise SplitProcessError(self.error)
        if not self.proc.is_alive():
            self.error = (f"Zerlege-Prozess (HydraHarp-Ereignisse) unerwartet beendet, Exit-Code {self.proc.exitcode} – "
                          "Lauf abgebrochen")
            raise SplitProcessError(self.error)

    def _ping(self, timeout_s: float) -> None:
        self._in.put(("ping", -1))
        t0 = time.monotonic()
        while True:
            try:
                it = self._out.get(timeout=0.2)
            except queue.Empty:
                self._check_alive()
                if time.monotonic() - t0 > timeout_s:
                    self.error = "Zerlege-Prozess antwortet nicht (Start)"
                    raise SplitProcessError(self.error) from None
                continue
            if it[0] == "pong":
                return
            self._take(it)

    def _take(self, it) -> None:
        if it[0] == "res":
            _, seq, slot, nh, nm, last, n = it
            if seq >= self.run_seq0:
                o = self._OUT[slot]
                self._ready.append(SplitResult(seq, o[:nh].copy(), o[nh:nh + nm].copy(), last, n))
            self._free.append(slot)
            self.n_done += 1
        elif it[0] == "end":
            self._ended.add(it[1])
        elif it[0] == "error":
            self.error = "Zerlege-Prozess (HydraHarp-Ereignisse) mit Fehler beendet:\n" + it[1]
            raise SplitProcessError(self.error)

    def _pump(self) -> None:
        while self._backlog:
            it = self._backlog[0]
            if it[0] == "data":
                if not self._free:
                    return
                slot = self._free.popleft()
                _, seq, rec = it
                self._IN[slot, :len(rec)] = rec
                self._in.put(("data", seq, slot, len(rec)))
                self.backlog_bytes -= rec.nbytes
            else:
                self._in.put(it)
            self._backlog.popleft()

    def _collect(self) -> None:
        while True:
            try:
                it = self._out.get_nowait()
            except queue.Empty:
                return
            self._take(it)

    # ------------------------------------------------------------------ Schnittstelle
    def begin_run(self, roles) -> None:
        """Neuer Lauf: Dekoder zurücksetzen. Reste eines abgebrochenen Laufs (Rückstau, späte Ergebnisse) werden
        nicht mehr zerlegt bzw. ignoriert – deren Rohdaten gehören zum verworfenen Lauf."""
        self._check_alive()
        self._collect()
        for it in [x for x in self._backlog if x[0] == "data"]:
            self.backlog_bytes -= it[2].nbytes
            self.n_done += 1
        self._backlog = deque(x for x in self._backlog if x[0] != "data")
        self._ready = []
        self.run_seq0 = self.seq
        self._backlog.append(("run", roles))
        self._pump()

    def submit(self, rec: np.ndarray) -> None:
        """Block übergeben, ohne zu warten (nie verworfen). SplitOverload bei zu großem Rückstau."""
        self._check_alive()
        rec = np.asarray(rec, dtype=np.uint32)
        for i in range(0, max(len(rec), 1), self.slot_rec):
            part = rec[i:i + self.slot_rec]
            if not len(part) and len(rec):
                break
            self._backlog.append(("data", self.seq, part))
            self.seq += 1
            self.backlog_bytes += part.nbytes
        self._collect()
        self._pump()
        self.max_backlog_seen = max(self.max_backlog_seen, self.backlog_bytes)
        if self.backlog_bytes > self.max_backlog:
            raise SplitOverload(f"Rückstau vor dem Zerlege-Prozess {self.backlog_bytes / 1e6:.0f} MB "
                                f"> {self.max_backlog / 1e6:.0f} MB – Ereignisrate zu hoch für diesen Rechner")

    def poll(self) -> list[SplitResult]:
        """Fertige Ergebnisse (in Reihenfolge), ohne zu warten."""
        self._collect()
        self._pump()
        self._check_alive()
        out, self._ready = self._ready, []
        return out

    def pending(self) -> int:
        return self.seq - self.n_done

    def finish_run(self, timeout_s: float = 60.0) -> list[SplitResult]:
        """Laufende: Rest zerlegen lassen und abholen (wartet, höchstens ``timeout_s``)."""
        tag = self.seq
        self._backlog.append(("end", tag))
        t0 = time.monotonic()
        while tag not in self._ended:
            self._pump()
            try:
                self._take(self._out.get(timeout=0.1))
            except queue.Empty:
                self._check_alive()
            if time.monotonic() - t0 > timeout_s:
                self.error = f"Zerlege-Prozess: Laufende nicht innerhalb {timeout_s:.0f} s erreicht"
                raise SplitProcessError(self.error)
        self._ended.discard(tag)
        out, self._ready = self._ready, []
        return out

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.proc.is_alive():
                self._in.put(None)
                self.proc.join(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        if self.proc.is_alive():
            self.proc.terminate()
            self.proc.join(timeout=5)
        for q in (self._in, self._out):
            q.cancel_join_thread()
            q.close()
        self._IN = self._OUT = None
        for m in (self._shm_in, self._shm_out):
            try:
                m.close()
                m.unlink()
            except Exception:  # noqa: BLE001
                pass

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


def synthetic_t2(rate_mcps: float, duration_s: float, seed: int = 1, herald_fraction: float = 0.9,
                 marker_hz: float = 1000.0) -> np.ndarray:
    """Synthetische T2-Records (Poisson, CH1 Herald / CH2 Rest, Sync-Marker) für Selbsttest und Messungen."""
    from . import ptu as P
    rng = np.random.default_rng(seed)
    T = int(duration_s * 1e12)
    n = int(rate_mcps * 1e6 * duration_s)
    t = np.sort(rng.integers(0, T, n))
    ch = np.where(rng.uniform(size=n) < herald_fraction, 1, 2)
    mk = np.arange(0, T, int(1e12 / marker_hz)) if marker_hz > 0 else np.empty(0, np.int64)
    times = np.concatenate([t, mk])
    kinds = np.concatenate([np.full(n, P.KIND_EVENT, np.int8), np.full(len(mk), P.KIND_SYNC, np.int8)])
    codes = np.concatenate([ch.astype(np.int8), np.ones(len(mk), np.int8)])
    o = np.argsort(times, kind="stable")
    times, kinds, codes = times[o], kinds[o], codes[o]
    del o, t, ch
    # stückweise kodieren (Speicher): Zeiten relativ zur Wrap-Periode des letzten Ereignisses davor
    parts, base, step = [], 0, 250_000
    for i in range(0, len(times), step):
        tp = times[i:i + step] - base * P.T2_WRAP
        parts.append(P.encode_t2(tp, kinds[i:i + step], codes[i:i + step])[0])
        base += int(tp[-1] // P.T2_WRAP)
    return np.concatenate(parts) if parts else np.empty(0, np.uint32)
