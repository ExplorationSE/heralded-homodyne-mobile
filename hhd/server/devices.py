"""Geräteabstraktion des Mess-Servers (Muster: ``harp/sources/base.py`` in GX, hier für zwei Geräte).

Jedes Gerät (HydraHarp 500, ADQ32) gibt es in drei Betriebsarten (Gesamtplan v1.7, ED-8b):

* ``live``   – echtes Gerät über das Hersteller-SDK. **Platzhalter:** die Klassen dokumentieren die
  benötigten SDK-Aufrufe und melden beim Öffnen einen klaren Fehler, solange kein Treiber eingebunden ist;
* ``sim``    – Simulator (``hhd.sim``) im Datenstrom-Modus, auf Wunsch in Echtzeit; die Dateien sind
  bitgleich zum Datei-Modus des Simulators;
* ``replay`` – Wiedergabe einer vorhandenen Sitzung aus den Dateien (PTU + ``digitizer.h5``).

Daten werden **dekodiert** übergeben (der Server braucht für Vorschau und Live-Synchronisation nur Zeiten
und Records): ``HarpData`` (Herald- und SYNC-Marker-Zeiten in Harp-Zeit) und ``DigData`` (Records,
Triggerzeiten und Marker in Digitizer-Zeit, optional Kanal B = LO-Monitor). Alle Methoden werden aus genau
einem Thread (dem Messablauf) aufgerufen.

Kritische Parameter (``critical=True``: Eingangsbereiche, Triggerpegel, Referenztakt, DC-Offset) dürfen nur am
Mess-PC oder mit ausdrücklicher Bestätigung geändert werden; Triggerausgänge (``local_only=True``) nie aus der
Ferne (ED-8b). Die Prüfung übernimmt der Server (``hhd.server.control.ParamPolicy``).
"""
from __future__ import annotations

import copy
import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

HARP_RES = 1e-12        # T2-Auflösung der HydraHarp (Zeitstempel in ps)


class DeviceError(RuntimeError):
    pass


@dataclass
class ParamSpec:
    name: str
    default: Any
    unit: str = ""
    doc: str = ""
    critical: bool = False          # nur am Mess-PC oder mit Bestätigung
    local_only: bool = False        # nie aus der Ferne (Triggerausgänge)
    choices: list | None = None
    lo: float | None = None
    hi: float | None = None

    def validate(self, v):
        if self.choices is not None:
            if v not in self.choices:
                raise ValueError(f"{self.name}: erlaubt {self.choices}")
            return v
        if isinstance(self.default, bool):
            if not isinstance(v, bool):
                raise ValueError(f"{self.name}: Wahrheitswert erwartet")
            return v
        if isinstance(self.default, (int, float)):
            try:
                x = type(self.default)(v)
            except (TypeError, ValueError):
                raise ValueError(f"{self.name}: Zahl erwartet") from None
            if not np.isfinite(float(x)):
                raise ValueError(f"{self.name}: endliche Zahl erwartet")
            if self.lo is not None and x < self.lo or self.hi is not None and x > self.hi:
                raise ValueError(f"{self.name}: Bereich {self.lo} … {self.hi} {self.unit}")
            return x
        return str(v)

    def to_dict(self) -> dict:
        return dict(name=self.name, default=self.default, unit=self.unit, doc=self.doc, critical=self.critical,
                    local_only=self.local_only, choices=self.choices, lo=self.lo, hi=self.hi)


HARP_PARAMS = [
    ParamSpec("sync_divider", 1, "", "Teiler des Sync-Eingangs", critical=True, choices=[1, 2, 4, 8, 16]),
    ParamSpec("sync_level_mV", -100.0, "mV", "Triggerpegel Sync-Eingang (CFD/Flanke)", critical=True, lo=-1200, hi=1200),
    ParamSpec("herald_level_mV", -100.0, "mV", "Triggerpegel Herald-Kanal", critical=True, lo=-1200, hi=1200),
    ParamSpec("herald_offset_ps", 0, "ps", "Kanalversatz Herald-Kanal", critical=True, lo=-99999, hi=99999),
    ParamSpec("ref_clock", "intern", "", "Referenztakt (intern | 10 MHz extern | White Rabbit)", critical=True,
              choices=["intern", "extern_10MHz", "white_rabbit"]),
    ParamSpec("marker_enable", True, "", "Marker-Eingänge (SYNC-Marker, Laufmarker)", critical=True),
    ParamSpec("trigger_output", False, "", "programmierbarer Triggerausgang (Laser/Schalter)", critical=True,
              local_only=True),
    ParamSpec("binning_display", 1, "", "Anzeige-Binning der Ratenanzeige (unkritisch)", choices=[1, 2, 4, 8]),
]

DIG_PARAMS = [
    ParamSpec("input_range_mV", 500.0, "mV", "Eingangsbereich Kanal A (Homodyn)", critical=True,
              choices=[100.0, 250.0, 500.0, 1000.0, 2500.0]),
    ParamSpec("input_range_b_mV", 1000.0, "mV", "Eingangsbereich Kanal B (LO-Monitor)", critical=True,
              choices=[250.0, 500.0, 1000.0, 2500.0]),
    ParamSpec("dc_offset_mV", 0.0, "mV", "DC-Offset Kanal A", critical=True, lo=-250, hi=250),
    ParamSpec("trigger_level_mV", 300.0, "mV", "Pegel des externen Triggers (TRIG-Eingang)", critical=True,
              lo=-2500, hi=2500),
    ParamSpec("ref_clock", "intern", "", "Referenztakt (intern | 10 MHz extern)", critical=True,
              choices=["intern", "extern_10MHz"]),
    ParamSpec("trigger_output", False, "", "Triggerausgang (SYNC-Marker an HydraHarp)", critical=True,
              local_only=True),
    ParamSpec("preview_records", 2000, "", "Records je Vorschau-Aktualisierung (unkritisch)", lo=100, hi=100000),
]


@dataclass
class RunSpec:
    """Ein Lauf des Laufplans (aus ``hhd.sim.config.BlockCfg`` bzw. dem Laufplan)."""
    block_id: str
    kind: str
    duration_s: float
    index: int = 0
    run_id: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(block_id=self.block_id, kind=self.kind, duration_s=self.duration_s, index=self.index,
                    run_id=self.run_id)


@dataclass
class HarpData:
    t_end_s: float                  # Laufzeit seit Laufbeginn (s), bis zu der Daten vorliegen
    herald_s: np.ndarray
    markers_s: np.ndarray
    n_records: int = 0
    run_done: bool = False


@dataclass
class DigData:
    data: np.ndarray                # (n, L) int16, Kanal A
    data_b: np.ndarray | None
    trig_D: np.ndarray
    first_D: np.ndarray
    markers_D: np.ndarray
    record_number: np.ndarray
    run_done: bool = False


class Device(ABC):
    kind = "device"
    mode = "?"
    model = "?"
    PARAMS: list[ParamSpec] = []

    def __init__(self):
        self.params = {p.name: p.default for p in self.PARAMS}
        self.is_open = False
        self.armed: RunSpec | None = None
        self.running = False

    def spec(self, name: str) -> ParamSpec:
        for p in self.PARAMS:
            if p.name == name:
                return p
        raise KeyError(name)

    def info(self) -> dict:
        return dict(kind=self.kind, mode=self.mode, model=self.model, open=self.is_open, running=self.running,
                    params=dict(self.params), param_specs=[p.to_dict() for p in self.PARAMS],
                    simulated=self.mode != "live")

    def configure(self, **p) -> dict:
        """Parameter prüfen und setzen (Berechtigung prüft der Server)."""
        new = {}
        for k, v in p.items():
            new[k] = self.spec(k).validate(v)
        if self.running:
            crit = [k for k in new if self.spec(k).critical]
            if crit:
                raise DeviceError(f"kritische Parameter {crit} nicht während eines Laufs")
        self.params.update(new)
        self._apply(new)
        return new

    def _apply(self, new: dict) -> None:
        pass

    @abstractmethod
    def open(self) -> dict: ...

    def close(self) -> None:
        self.is_open = False

    def arm(self, run: RunSpec) -> None:
        self.armed = run

    def start(self) -> None:
        if self.armed is None:
            raise DeviceError("kein Lauf vorbereitet")
        self.running = True

    @abstractmethod
    def read(self): ...

    def stop(self) -> None:
        self.running = False


# ------------------------------------------------------------------ live (Platzhalter)
class HydraHarp500Live(Device):
    """Platzhalter für die HydraHarp 500 über PicoQuant **snAPI** bzw. ``HH500Lib``.

    Benötigte Aufrufe (Abbildung der Methoden): ``open`` → ``HH500_OpenDevice``/``Initialize(mode=T2,
    refsource)``; ``configure`` → ``SetSyncDiv``, ``SetSyncEdgeTrg``/``SetInputEdgeTrg`` (Pegel), ``SetInputChannelOffset``,
    ``SetMarkerEnable``, ``SetTriggerOutput`` (nur lokal); ``start`` → ``StartMeas``; ``read`` → ``ReadFiFo``
    (T2-Records, dekodiert mit ``hhd.io.ptu.T2Decoder``; PTU-Datei parallel geschrieben); ``stop`` → ``StopMeas``.
    """
    kind, mode, model = "harp", "live", "PicoQuant HydraHarp 500"
    PARAMS = HARP_PARAMS

    def open(self) -> dict:
        raise DeviceError("HydraHarp 500: kein Treiber eingebunden (Platzhalter, snAPI/HH500Lib fehlt). "
                          "Betriebsart 'sim' oder 'replay' wählen.")

    def read(self):
        raise DeviceError("nicht geöffnet")


class ADQ32Live(Device):
    """Platzhalter für den Teledyne SP Devices ADQ32 über **ADQAPI** (``pyadq``).

    Abbildung: ``open`` → ``CreateADQControlUnit``/``SetupDevice``; ``configure`` → ``GetParameters``/``SetParameters``
    (``ADQ_PARAMETER_ID_TOP``: Eingangsbereich, DC-Offset, Triggerpegel/-quelle, Referenztakt, Recordlänge,
    horizontaler Versatz, Marker auf GPIO); ``start`` → ``StartDataAcquisition``; ``read`` → ``WaitForRecordBuffer``
    (Records + Header: Zeitstempel, record_start, Triggerquelle; Marker aus dem GPIO-Ereigniskanal), Dateien wie
    ``hhd.io.schema`` (``digitizer.h5``); ``stop`` → ``StopDataAcquisition``.
    """
    kind, mode, model = "digitizer", "live", "Teledyne SP Devices ADQ32"
    PARAMS = DIG_PARAMS

    def open(self) -> dict:
        raise DeviceError("ADQ32: kein Treiber eingebunden (Platzhalter, ADQAPI/pyadq fehlt). "
                          "Betriebsart 'sim' oder 'replay' wählen.")

    def read(self):
        raise DeviceError("nicht geöffnet")


# ------------------------------------------------------------------ Simulation
class SimBackend:
    """Gemeinsamer Simulator beider Geräte: simuliert eine Sitzung und spielt sie laufweise in Echtzeit ab.

    Da Harp und Digitizer im Experiment über SYNC-Marker gekoppelt sind, kommen beide Datenströme aus derselben
    Simulation; jedes Gerät liest seinen Anteil aus einer eigenen Warteschlange.
    """

    def __init__(self, base_config: str | Path | None = None, chunk_s: float = 0.1, speed: float = 1.0,
                 realtime: bool = True, time_scale: float | None = None):
        self.base_config = Path(base_config) if base_config else None
        self.chunk_s, self.speed, self.realtime = chunk_s, speed, realtime
        self.time_scale = time_scale
        self.res = None
        self.cfg = None
        self._it = None
        self._q = {"harp": [], "digitizer": []}
        self._done_block: str | None = None
        self._decoder = None
        self._roles = ({}, {})

    # Konfiguration der Sitzung
    def make_config(self, session_id: str, seed: int, plan: str = "standard"):
        from ..sim.config import SimConfig
        cfg = SimConfig.load(self.base_config) if self.base_config else SimConfig()
        cfg.session.id = session_id
        cfg.session.seed = int(seed)
        cfg.session.campaign = "K-SERVER"
        if plan == "kurz":                    # schnelle Prüfsitzung: K-V, Dunkel, K-H, A, B, A, K-V
            cfg.blocks = short_plan(cfg.blocks)
        if self.time_scale:
            for b in cfg.blocks:
                b.duration_s = float(b.duration_s) * self.time_scale
        return cfg

    def prepare(self, cfg) -> list[RunSpec]:
        from ..sim.engine import simulate
        self.cfg = cfg
        self.res = simulate(cfg)
        return [RunSpec(b.cfg.id, b.cfg.kind, b.duration, i, b.run_id) for i, b in enumerate(self.res.blocks)]

    @property
    def device_attrs(self) -> dict:
        return self.res.device_attrs if self.res else {}

    def start_run(self, idx: int) -> None:
        from ..io.ptu import T2Decoder
        from ..sim.stream import SimStream
        br = self.res.blocks[idx]
        self._br = br
        sub = copy.copy(self.res)
        sub.blocks = [br]
        self._it = iter(SimStream(sub, chunk_s=self.chunk_s, speed=self.speed, realtime=self.realtime))
        self._q = {"harp": [], "digitizer": []}
        self._decoder = T2Decoder()
        self._roles = ({int(k): v for k, v in json.loads(br.attrs["harp_channel_roles"]).items()},
                       {int(k): v for k, v in json.loads(br.attrs["harp_marker_roles"]).items()})
        self._tu = self.res.device_attrs["time_unit_s"]

    def _pull(self) -> bool:
        from ..io.session import split_events
        try:
            c = next(self._it)
        except (StopIteration, TypeError):
            return False
        ev = self._decoder.decode(c.harp_records)
        hs = split_events(ev, *self._roles)
        self._q["harp"].append(HarpData(c.t_end, hs.role_s("herald"), hs.role_s("sync_marker"),
                                        len(c.harp_records), c.last_in_block))
        tu = self._tu
        self._q["digitizer"].append(DigData(c.rec_data, self._data_b(c), c.rec_timestamp.astype(float) * tu,
                                            (c.rec_timestamp + c.rec_record_start).astype(float) * tu,
                                            np.sort(c.marker_timestamp).astype(float) * tu, c.rec_number,
                                            c.last_in_block))
        return True

    def _data_b(self, c):
        db = self._br.digitizer.data_b
        return None if db is None else db[c.rec_number]

    def read(self, kind: str):
        if not self._q[kind]:
            if not self._pull():
                return None
        return self._q[kind].pop(0)

    def write_files(self, out_dir: Path, n_done: int | None = None) -> dict:
        from ..sim.engine import write_run
        res = self.res
        if n_done is not None and n_done < len(res.blocks):
            res = copy.copy(res)
            res.blocks = res.blocks[:n_done]
        return write_run(res, out_dir)


def short_plan(blocks: list) -> list:
    """Kurzer Laufplan aus dem vollen: erster K-V, Dunkellauf, K-H, A, B, A, letzter K-V (Reihenfolge bleibt)."""
    want = {"vacuum": 1, "electronic": 1, "herald_blocked": 1, "heralded": 2, "free_running": 1, "random": 1}
    keep, cnt = [], {}
    for b in blocks:
        if cnt.get(b.kind, 0) < want.get(b.kind, 0):
            keep.append(b); cnt[b.kind] = cnt.get(b.kind, 0) + 1
    last_kv = [b for b in blocks if b.kind == "vacuum"]
    if last_kv and last_kv[-1] not in keep:
        keep.append(last_kv[-1])
    # A–B–A: ein zweites A nach B verschieben
    hs = [b for b in keep if b.kind == "heralded"]
    bs = [b for b in keep if b.kind in ("free_running", "random")]
    if len(hs) == 2 and bs:
        keep.remove(hs[1]); keep.insert(keep.index(bs[0]) + 1, hs[1])
    return keep


class _SimDevice(Device):
    mode = "sim"

    def __init__(self, backend: SimBackend):
        super().__init__()
        self.backend = backend

    def open(self) -> dict:
        self.is_open = True
        return self.info()

    def start(self) -> None:
        super().start()

    def read(self):
        if not self.running:
            return None
        return self.backend.read(self.kind)


class SimHarp(_SimDevice):
    kind, model = "harp", "HydraHarp 500 (simuliert)"
    PARAMS = HARP_PARAMS


class SimDigitizer(_SimDevice):
    kind, model = "digitizer", "ADQ32 (simuliert)"
    PARAMS = DIG_PARAMS


# ------------------------------------------------------------------ Wiedergabe
class ReplayBackend:
    """Wiedergabe einer vorhandenen Sitzung (Dateien) als Datenstrom, laufweise, optional mit Echtzeitfaktor."""

    def __init__(self, session_dir: str | Path, chunk_s: float = 0.1, speed: float = 1.0, realtime: bool = True):
        self.session_dir = Path(session_dir)
        self.chunk_s, self.speed, self.realtime = chunk_s, speed, realtime
        self.session = None
        self._q = {"harp": [], "digitizer": []}

    def prepare(self, cfg=None) -> list[RunSpec]:
        from ..io.session import load_session
        self.session = load_session(self.session_dir)
        out = []
        for i, r in enumerate(self.session.runs):
            out.append(RunSpec(r.block_id, r.kind, float(r.duration_s or 0.0), i, r.run_id))
        return out

    @property
    def device_attrs(self) -> dict:
        return dict(self.session.device) if self.session else {}

    def start_run(self, idx: int) -> None:
        import time
        run = self.session.runs[idx]
        hs = run.harp
        her, mk = hs.role_s("herald"), hs.role_s("sync_marker")
        t0 = mk[0] if len(mk) else (her[0] if len(her) else 0.0)
        TDm = run.markers_D(); trig = run.trigger_D(); first = run.first_sample_D()
        T0D = TDm[0] if len(TDm) else (trig[0] if len(trig) else 0.0)
        T = max(float((mk[-1] - t0) if len(mk) > 1 else 0.0), float((trig[-1] - T0D) if len(trig) else 0.0), 1e-3)
        n = max(1, int(np.ceil(T / self.chunk_s)))
        self._chunks = []
        for k in range(n):
            a, b = k * self.chunk_s, (k + 1) * self.chunk_s if k < n - 1 else np.inf
            hm = (her - t0 >= a) & (her - t0 < b); mm = (mk - t0 >= a) & (mk - t0 < b)
            rm = np.nonzero((trig - T0D >= a) & (trig - T0D < b))[0]
            dm = (TDm - T0D >= a) & (TDm - T0D < b)
            self._chunks.append((min(b, T), her[hm], mk[mm], rm, TDm[dm], k == n - 1))
        self._run, self._trig, self._first = run, trig, first
        self._k = 0
        self._t_wall0 = time.monotonic()

    def _pull(self) -> bool:
        import time
        if self._k >= len(self._chunks):
            return False
        t1, her, mk, rm, TDm, last = self._chunks[self._k]
        if self.realtime and np.isfinite(t1):
            wait = self._t_wall0 + t1 / self.speed - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        self._k += 1
        self._q["harp"].append(HarpData(t1, her, mk, len(her) + len(mk), last))
        run = self._run
        if len(rm):
            data = run.read_records(rm, channel="A")
            try:
                db = run.read_records(rm, channel="B")
            except Exception:  # noqa: BLE001 – Kanal B optional
                db = None
        else:
            data, db = np.empty((0, 0), np.int16), None
        self._q["digitizer"].append(DigData(data, db, self._trig[rm], self._first[rm], TDm, rm.astype(np.uint32), last))
        return True

    def read(self, kind: str):
        if not self._q[kind]:
            if not self._pull():
                return None
        return self._q[kind].pop(0)

    def write_files(self, out_dir: Path, n_done: int | None = None) -> dict:
        """Wiedergabe: die Quelldateien werden per Verweis (Symlink) in die neue Sitzung übernommen; ``truth.h5``
        wird nie übernommen (Trennung Wahrheit/Auswertung)."""
        out_dir.mkdir(parents=True, exist_ok=True)
        files = {}
        for f in sorted(self.session_dir.iterdir()):
            if f.is_file() and f.name != "truth.h5":
                dst = out_dir / f.name
                if not dst.exists():
                    os.symlink(f.resolve(), dst)
                files[f.name] = str(dst)
        return files


class ReplayHarp(_SimDevice):
    kind, mode, model = "harp", "replay", "HydraHarp 500 (Wiedergabe)"
    PARAMS = HARP_PARAMS


class ReplayDigitizer(_SimDevice):
    kind, mode, model = "digitizer", "replay", "ADQ32 (Wiedergabe)"
    PARAMS = DIG_PARAMS


def make_devices(mode: str, base_config=None, replay_dir=None, chunk_s: float = 0.1, speed: float = 1.0,
                 realtime: bool = True, time_scale: float | None = None):
    """(backend, harp, digitizer) für ``mode`` ∈ {live, sim, replay}."""
    if mode == "sim":
        be = SimBackend(base_config, chunk_s, speed, realtime, time_scale)
        return be, SimHarp(be), SimDigitizer(be)
    if mode == "replay":
        if not replay_dir:
            raise DeviceError("replay: Verzeichnis der Sitzung fehlt (devices.replay_dir)")
        be = ReplayBackend(replay_dir, chunk_s, speed, realtime)
        return be, ReplayHarp(be), ReplayDigitizer(be)
    if mode == "live":
        return None, HydraHarp500Live(), ADQ32Live()
    raise DeviceError(f"unbekannte Betriebsart {mode!r}")
