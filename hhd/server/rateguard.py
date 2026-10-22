"""Ratenbegrenzung der HydraHarp-Ereignisse (ab 0.15).

* **Selbsttest** (:func:`selftest`) beim Serverstart: synthetischer T2-Strom bei 85 Mcps, gemessen werden der
  Durchsatz des Zerlege-Prozesses (``hhd.io.t2split``, inklusive Übergabe über Shared Memory) und das Schreiben einer
  PTU-Datei im Datenverzeichnis (mit ``fsync``). Grenzwert = Sicherheitsfaktor × Minimum, höchstens 85 Mcps
  (dauerhafter Durchsatz der HydraHarp 500 über USB). Ergebnis je Prozess zwischengespeichert.
* **Grenzwert** aus Konfiguration (``harp_max_mcps`` > 0 ersetzt den Selbsttest) oder Umgebung ``HHD_HARP_MAX_MCPS``.
* **Überwachung** (:class:`RateGuard`): gleitende Zählrate über ``window_s`` Messzeit. Über dem Grenzwert: Warnung
  (App, Protokoll); länger als ``stop_s`` ununterbrochen: Lauf anhalten mit klarer Meldung. Es wird nie still
  verworfen – bis zum Anhalten werden alle Ereignisse geschrieben und zerlegt.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

HARP_MAX_SUSTAINED_MCPS = 85.0          # HydraHarp 500, alle Kanäle, USB 3.0 (Datenblatt)

HINTS = ("Zählrate senken: Abschwächer/ND-Filter vor den Detektoren, Herald-/Eingangsschwelle anheben "
         "(herald_level_mV, aux_level_mV, CFD), nicht benötigte Eingänge abschalten (Kanalrollen im Laufplan), "
         "Detektor-Totzeit bzw. HydraHarp-Eingangstotzeit erhöhen (HH500_SetInputDeadTime, siehe docs/HARDWARE.md), "
         "oder Grenzwert auf einem stärkeren Rechner neu messen.")

_CACHE: dict = {}
_LOCK = threading.Lock()


def selftest(data_dir: str | Path | None = None, safety: float = 0.6, duration_s: float = 0.05,
             repeats: int = 6, force: bool = False) -> dict:
    """Kurzer Selbsttest (≈ 2–4 s). Rückgabe: split_mcps, write_mcps, limit_mcps, Dauer, Fehler."""
    key = (str(data_dir), float(safety))
    with _LOCK:
        if key in _CACHE and not force:
            return dict(_CACHE[key], cached=True)
    import numpy as np
    from ..io.stream_writer import PtuStreamWriter
    from ..io.t2split import T2SplitProcess, synthetic_t2
    t_start = time.perf_counter()
    out = dict(safety=safety, rate_mcps=HARP_MAX_SUSTAINED_MCPS, duration_s=duration_s, repeats=repeats)
    rec = synthetic_t2(HARP_MAX_SUSTAINED_MCPS, duration_s, seed=7)
    n = len(rec) * repeats
    # Zerlege-Prozess: so schnell wie möglich einspeisen, bis alles zurück ist
    sp = T2SplitProcess(max_backlog_mb=1e6)
    try:
        sp.begin_run(({0: "sync_marker", 1: "herald"}, {}))
        sp.finish_run(30)
        t0 = time.perf_counter()
        for _ in range(repeats):
            sp.submit(rec)
            sp.poll()                                  # Ergebnisse sofort verwerfen (Speicher)
        t_end = time.monotonic() + 120
        while sp.pending() and time.monotonic() < t_end:
            sp.poll()
            time.sleep(0.0005)
        sp.finish_run(max(1.0, t_end - time.monotonic()))
        out["split_mcps"] = n / (time.perf_counter() - t0) / 1e6
    finally:
        sp.close()
    # Schreiben: PTU im Datenverzeichnis, inkl. fsync
    d = Path(data_dir) if data_dir else Path(tempfile.gettempdir())
    d.mkdir(parents=True, exist_ok=True)
    p = d / f".hhd_selftest_{os.getpid()}.ptu"
    try:
        t0 = time.perf_counter()
        w = PtuStreamWriter(p)
        for _ in range(repeats):
            w.write(rec)
        w._fh.flush()
        os.fsync(w._fh.fileno())
        w.close()
        out["write_mcps"] = n / (time.perf_counter() - t0) / 1e6
    finally:
        try:
            p.unlink()
        except OSError:
            pass
    out["write_MBps"] = out["write_mcps"] * 4
    base = min(out["split_mcps"], out["write_mcps"])
    out["limit_mcps"] = round(min(safety * base, HARP_MAX_SUSTAINED_MCPS), 2)
    out["bottleneck"] = "zerlegen" if out["split_mcps"] <= out["write_mcps"] else "schreiben"
    out["test_s"] = round(time.perf_counter() - t_start, 2)
    out["records"] = int(n)
    out["time"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _LOCK:
        _CACHE[key] = out
    del np
    return dict(out, cached=False)


@dataclass
class RateGuard:
    """Gleitende Zählrate (Messzeit) gegen den Grenzwert; ``feed`` liefert "ok" | "warn" | "stop"."""
    limit_mcps: float
    window_s: float = 1.0
    stop_s: float = 10.0
    hist: list = field(default_factory=list)          # (t_s, N kumuliert)
    n: int = 0
    rate_mcps: float = 0.0
    peak_mcps: float = 0.0
    over_since: float | None = None
    over_s_total: float = 0.0
    state: str = "ok"
    n_warn: int = 0

    def reset(self) -> None:
        self.hist, self.n, self.rate_mcps, self.over_since, self.state = [(0.0, 0)], 0, 0.0, None, "ok"
        self.peak_mcps = 0.0
        self.over_s_total = 0.0

    def feed(self, n_records: int, t_s: float) -> str:
        if not self.hist:
            self.reset()
        self.n += int(n_records)
        t_s = max(float(t_s), self.hist[-1][0])
        if t_s > self.hist[-1][0] or n_records:
            self.hist.append((t_s, self.n))
        while len(self.hist) > 2 and self.hist[1][0] <= t_s - self.window_s:
            self.hist.pop(0)
        t0, n0 = self.hist[0]
        dt = t_s - t0
        if dt <= 0:
            return self.state
        self.rate_mcps = (self.n - n0) / dt / 1e6
        if dt >= min(self.window_s, 0.05):
            self.peak_mcps = max(self.peak_mcps, self.rate_mcps)
        if self.limit_mcps > 0 and self.rate_mcps > self.limit_mcps:
            if self.over_since is None:
                self.over_since = t_s
                self.n_warn += 1
            self.over_s_total = t_s - self.over_since
            self.state = "stop" if t_s - self.over_since >= self.stop_s else "warn"
        else:
            self.over_since = None
            self.state = "ok"
        return self.state

    def to_dict(self) -> dict:
        return dict(limit_mcps=self.limit_mcps, rate_mcps=round(self.rate_mcps, 3), peak_mcps=round(self.peak_mcps, 3),
                    state=self.state, over_s=round(self.over_s_total, 2) if self.over_since is not None else 0.0,
                    stop_s=self.stop_s, window_s=self.window_s)
