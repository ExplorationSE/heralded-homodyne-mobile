"""Geräteabstraktion des Mess-Servers (Muster: ``harp/sources/base.py`` in GX, hier für zwei Geräte).

Jedes Gerät (HydraHarp 500, ADQ32) gibt es in drei Betriebsarten (Gesamtplan v1.7, ED-8b):

* ``live``   – echtes Gerät über die Herstellerbibliothek (``hhd.drivers``: HH500Lib bzw. ADQAPI, zur Laufzeit
  geladen; Klassen in ``hhd.server.livedev``). Fehlt die Bibliothek, meldet ``open`` einen klaren Fehler;
* ``sim``    – Simulator (``hhd.sim``) im Datenstrom-Modus, auf Wunsch in Echtzeit; die Dateien sind
  bitgleich zum Datei-Modus des Simulators;
* ``replay`` – Wiedergabe einer vorhandenen Sitzung aus den Dateien (PTU + ``digitizer.h5``).

Jedes Gerät hat seine eigene Betriebsart (gemischter Betrieb, z. B. HydraHarp live + ADQ32 simuliert); die
Betriebsart steht in den Metadaten jeder Ausgabedatei (``hhd.io.modes``).

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
    ParamSpec("trg_mode", "ETR", "", "Triggerart aller Eingänge: ETR (Flanke) | CFD", critical=True,
              choices=["ETR", "CFD"]),
    ParamSpec("edge", "falling", "", "Flanke bei ETR (NIM-Pulse: falling)", critical=True, choices=["falling", "rising"]),
    ParamSpec("cfd_zerocross_mV", 10, "mV", "CFD-Nulldurchgangspegel", critical=True, lo=-100, hi=100),
    ParamSpec("sync_offset_ps", 0, "ps", "Kanalversatz Sync-Eingang", critical=True, lo=-99999, hi=99999),
    ParamSpec("aux_level_mV", -100.0, "mV", "Triggerpegel CH2–CH4 (Triggerkopie, Hilfskanäle)", critical=True,
              lo=-1200, hi=1200),
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
    ParamSpec("dc_offset_b_mV", 0.0, "mV", "DC-Offset Kanal B (unipolarer Bereich des LO-Monitors)", critical=True,
              lo=-250, hi=250),
    ParamSpec("sync_level_mV", 500.0, "mV", "Pegel des SYNC-Eingangs (10-MHz-Marker, ED-7)", critical=True,
              lo=-2500, hi=2500),
    ParamSpec("record_length", 5000, "Samples", "Recordlänge Kanal A (Kanal B: / Sample Skip)", critical=True,
              lo=64, hi=65536),
    ParamSpec("pretrigger", 2000, "Samples", "Pretrigger Kanal A (negativer horizontal_offset)", critical=True,
              lo=0, hi=16360),
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
    records: np.ndarray | None = None   # rohe T2-Records (uint32) für den Datenstrom-Schreiber


@dataclass
class DigData:
    data: np.ndarray                # (n, L) int16, Kanal A
    data_b: np.ndarray | None
    trig_D: np.ndarray
    first_D: np.ndarray
    markers_D: np.ndarray
    record_number: np.ndarray
    run_done: bool = False
    raw: dict | None = None             # timestamp, record_start (time_unit), trigger_source, markers, data_b, marker_b


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
                    simulated=self.mode == "sim")

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


# ------------------------------------------------------------------ Simulation
class SimBackend:
    """Gemeinsamer Simulator beider Geräte: simuliert eine Sitzung und spielt sie laufweise in Echtzeit ab.

    Da Harp und Digitizer im Experiment über SYNC-Marker gekoppelt sind, kommen beide Datenströme aus derselben
    Simulation; jedes Gerät liest seinen Anteil aus einer eigenen Warteschlange.
    """

    engine = "batch"
    records_stream = False

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
        self.kinds = {"harp", "digitizer"}    # gemischter Betrieb: nur die simulierten Geräte lesen

    # Konfiguration der Sitzung
    def make_config(self, session_id: str, seed: int, plan: str = "standard"):
        from ..sim.config import SimConfig
        cfg = SimConfig.load(self.base_config) if self.base_config else SimConfig()
        cfg.session.id = session_id
        cfg.session.seed = int(seed)
        cfg.session.campaign = "K-SERVER"
        if plan == "kurz":                    # schnelle Prüfsitzung: K-V, Dunkel, K-H, A, B, A, K-V
            cfg.blocks = short_plan(cfg.blocks)
        elif plan.startswith("kurzphase_"):   # mit Phase (Piezo-Rampe, K-θ), hhd.sim.state_plans
            from ..sim.state_plans import PHASE_PLANS, apply_phase_plan
            if plan not in PHASE_PLANS:
                raise ValueError(f"unbekannter Laufplan {plan!r}")
            apply_phase_plan(cfg, plan)
        elif plan.startswith("kurz_"):        # kurze Sitzung für weitere Zustände (hhd.sim.state_plans)
            from ..sim.state_plans import STATE_PLANS, apply_state_plan
            if plan not in STATE_PLANS:
                raise ValueError(f"Laufplan {plan!r} unbekannt")
            apply_state_plan(cfg, plan)
        if self.time_scale:
            for b in cfg.blocks:
                b.duration_s = float(b.duration_s) * self.time_scale
        return cfg

    def prepare(self, cfg) -> list[RunSpec]:
        from ..sim.engine import simulate
        self.cfg = cfg
        self.res = simulate(cfg)
        return [RunSpec(b.cfg.id, b.cfg.kind, b.duration, i, b.run_id, dict(attrs=dict(b.attrs)))
                for i, b in enumerate(self.res.blocks)]

    @property
    def device_attrs(self) -> dict:
        return self.res.device_attrs if self.res else {}

    def session_meta(self) -> dict:
        r = self.res
        return dict(session_id=r.session_id, campaign_id=r.campaign_id, calibration_id=r.calibration_id,
                    seed=r.cfg.session.seed)

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
        if "harp" in self.kinds:
            ev = self._decoder.decode(c.harp_records)
            hs = split_events(ev, *self._roles)
            self._q["harp"].append(HarpData(c.t_end, hs.role_s("herald"), hs.role_s("sync_marker"),
                                            len(c.harp_records), c.last_in_block, records=c.harp_records))
        if "digitizer" in self.kinds:
            tu = self._tu
            db = self._data_b(c)
            rr = self._br.digitizer.ramp_raw
            raw = dict(timestamp=c.rec_timestamp, record_start=c.rec_record_start, trigger_source=c.rec_trigger_source,
                       markers=c.marker_timestamp, data_b=db, marker_b=None,
                       aux=dict(ramp_raw=rr[c.rec_number]) if rr is not None else None)
            self._q["digitizer"].append(DigData(c.rec_data, db, c.rec_timestamp.astype(float) * tu,
                                                (c.rec_timestamp + c.rec_record_start).astype(float) * tu,
                                                np.sort(c.marker_timestamp).astype(float) * tu, c.rec_number,
                                                c.last_in_block, raw=raw))
        return True

    def _data_b(self, c):
        db = self._br.digitizer.data_b
        return None if db is None else db[c.rec_number]

    def read(self, kind: str):
        while not self._q[kind]:
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


class StreamSimBackend(SimBackend):
    """Simulator im Datenstrom (``hhd.sim.streaming.StreamingSimulator``): jeder Lauf wird in Zeitscheiben von
    ``chunk_s`` erzeugt und sofort ausgegeben; der Server schreibt die Dateien wie im Live-Betrieb mit
    ``SessionRecorder``. Der Speicher hängt nicht von Lauf- oder Sitzungsdauer ab (``sim_engine = "stream"``).

    Unterschiede zum Datei-Simulator (``SimBackend``): Laufmarken und Phasenreferenz-Marker fehlen, Records an
    Scheibengrenzen entfallen, ``truth.h5`` dezimiert (``docs/SIMULATOR.md`` Abschnitt 7)."""

    engine = "stream"
    records_stream = True                  # Dateien schreibt der Server (SessionRecorder), nicht write_files

    def prepare(self, cfg) -> list[RunSpec]:
        from ..sim.streaming import StreamingSimulator
        self.cfg = cfg
        self.sim = StreamingSimulator(cfg, slice_s=self._slice_s(cfg))
        runs = self.sim.setup()
        self._attrs = self.sim.device_attrs
        return [RunSpec(r.block_id, r.kind, r.duration_s, r.index, r.run_id, dict(r.extra)) for r in runs]

    def _slice_s(self, cfg) -> float:
        """Kleinste Scheibe ≥ ``chunk_s``, mit der Marker- und Periodenraster ganzzahlig aufgehen."""
        rates = [cfg.markers.f_marker] + [b.periodic_rate for b in cfg.blocks
                                          if b.kind in ("vacuum", "electronic", "free_running")]
        for k in range(1, 1001):
            sl = round(k * 0.001, 6)
            if sl + 1e-12 >= self.chunk_s and all(abs(r * sl - round(r * sl)) < 1e-9 for r in rates):
                return sl
        raise DeviceError(f"keine passende Scheibenlänge für chunk_s = {self.chunk_s}")

    @property
    def device_attrs(self) -> dict:
        return dict(self._attrs) if getattr(self, "sim", None) else {}

    def session_meta(self) -> dict:
        return dict(self.sim.meta, seed=self.cfg.session.seed)

    def extra_manifest(self) -> dict:
        return dict(self.sim.extra_manifest)

    def start_run(self, idx: int) -> None:
        import time
        from ..io.ptu import T2Decoder
        run = self.sim.runs[idx]
        self._it = self.sim.iter_run(idx)
        self._q = {"harp": [], "digitizer": []}
        self._decoder = T2Decoder()
        a = run.extra["attrs"]
        self._roles = ({int(k): v for k, v in json.loads(a["harp_channel_roles"]).items()},
                       {int(k): v for k, v in json.loads(a["harp_marker_roles"]).items()})
        self._tu = self._attrs["time_unit_s"]
        self._t_wall0 = time.monotonic()

    def _pull(self) -> bool:
        import time
        from ..io.session import split_events
        try:
            info, hrec, dg = next(self._it)
        except (StopIteration, TypeError):
            return False
        t1, last = info["t_run_end"], info["last"]
        if self.realtime:
            wait = self._t_wall0 + t1 / self.speed - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        if "harp" in self.kinds:
            hs = split_events(self._decoder.decode(hrec), *self._roles)
            her, mk = hs.role_s("herald"), hs.role_s("sync_marker")
            self._q["harp"].append(HarpData(t1, her, mk, len(her) + len(mk), last, records=hrec))
        if "digitizer" in self.kinds:
            tu = self._tu
            ts, rs = dg.timestamp.astype(np.int64), dg.record_start.astype(np.int64)
            raw = dict(timestamp=dg.timestamp, record_start=dg.record_start, trigger_source=dg.trigger_source,
                       markers=dg.marker_ts, data_b=dg.data_b, marker_b=None,
                       aux=dict(ramp_raw=dg.ramp_raw) if dg.ramp_raw is not None else None)
            self._rn = getattr(self, "_rn", 0) if info["slice"] else 0
            rn = np.arange(self._rn, self._rn + len(ts), dtype=np.uint32)
            self._rn += len(ts)
            self._q["digitizer"].append(DigData(dg.data, dg.data_b, ts.astype(float) * tu, (ts + rs).astype(float) * tu,
                                                np.sort(np.asarray(dg.marker_ts, np.int64)).astype(float) * tu, rn,
                                                last, raw=raw))
        return True

    def set_output(self, out_dir: Path, record_truth: bool = False) -> None:
        """Vor dem ersten Lauf: Sitzungsverzeichnis; mit ``record_truth`` schreibt der Simulator die
        Zuordnungswahrheit je Record (``truth_records.h5``, für ``hhd validate-records``)."""
        self.sim.record_truth_dir = Path(out_dir) if record_truth else None

    def write_files(self, out_dir: Path, n_done: int | None = None) -> dict:
        """Nach ``SessionRecorder.close``: ``truth.h5`` der fertigen Läufe und Manifest-Ergänzungen."""
        done = {r.block_id for r in self.sim.runs[: n_done if n_done is not None else len(self.sim.runs)]}
        man = self.sim.write_truth(out_dir, run_ids=done)
        return dict(man.get("files", {}))


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

    def info(self) -> dict:
        out = super().info()
        eng = getattr(self.backend, "engine", None)
        if self.mode == "sim" and eng:
            out["sim_engine"] = eng
            if eng == "stream":
                out["model"] = self.model.replace("(simuliert)", "(simuliert, Datenstrom)")
        return out

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
        self.kinds = {"harp", "digitizer"}
        self._run = None

    def prepare(self, cfg=None) -> list[RunSpec]:
        from ..io.session import load_session
        self.session = load_session(self.session_dir, lazy=True)     # Köpfe erst je Lauf, blockweise
        out = []
        for i, r in enumerate(self.session.runs):
            out.append(RunSpec(r.block_id, r.kind, float(r.duration_s or 0.0), i, r.run_id, dict(attrs=dict(r.attrs))))
        return out

    @property
    def device_attrs(self) -> dict:
        return dict(self.session.device) if self.session else {}

    def session_meta(self) -> dict:
        ra = self.session.root_attrs
        return dict(session_id=str(ra.get("session_id", "")), campaign_id=str(ra.get("campaign_id", "")),
                    calibration_id=str(ra.get("calibration_id", "")), source_session=str(self.session_dir),
                    source_simulated=bool(self.session.manifest.get("simulated", True)))

    def start_run(self, idx: int) -> None:
        """Lauf als Folge von Zeitfenstern (``chunk_s``) öffnen; PTU und digitizer.h5 werden blockweise gelesen
        (``hhd.io.chunked``), der Speicher hängt nicht von der Laufdauer ab."""
        import time
        from ..io import chunked as CK
        from ..io import ptu as P
        if getattr(self, "_run", None) is not None:
            self._run.release()
        run = self.session.runs[idx]
        tu = run.time_unit
        t0 = self._first_harp_s(run)
        T0D_tu = self._first_dig_tu(run)
        self._t0, self._T0D = t0, T0D_tu * tu
        w_ps, w_tu = int(round(self.chunk_s * 1e12)), max(1, int(round(self.chunk_s / tu)))
        streams = []
        if "harp" in self.kinds:
            streams.append(("harp", CK.iter_ptu_windows(run.ptu_path, int(round(t0 * 1e12)), w_ps)))
        if "digitizer" in self.kinds:
            streams.append(("rec", CK.iter_record_windows(run.session.h5_path, run.block_id, T0D_tu, w_tu)))
            streams.append(("mk", CK.iter_marker_windows(run.session.h5_path, run.block_id, T0D_tu, w_tu,
                                                         with_b=run.has_marker_b)))
        self._streams = streams
        self._dec = P.T2Decoder()
        self._run = run
        self._k = 0
        self._next = self._advance()
        self._t_wall0 = time.monotonic()

    @staticmethod
    def _first_harp_s(run) -> float:
        from ..io import ptu as P
        from ..io.session import split_events
        dec = P.T2Decoder()
        her0 = None
        for i, rec in enumerate(P.iter_ptu_records(run.ptu_path, 1 << 16)):
            hs = split_events(dec.decode(rec), run.channel_roles, run.marker_roles)
            mk = hs.role_s("sync_marker")
            if len(mk):
                return float(mk.min())
            h = hs.role_s("herald")
            if her0 is None and len(h):
                her0 = float(h.min())
            if i >= 64:
                break
        return her0 if her0 is not None else 0.0

    @staticmethod
    def _first_dig_tu(run) -> int:
        import h5py
        with h5py.File(run.session.h5_path, "r") as f:
            g = f["blocks"][run.block_id]
            m = g["markers/timestamp"]
            if m.shape[0]:
                return int(np.min(m[: min(m.shape[0], 1 << 16)]))
            ts = g["records/timestamp"]
            return int(ts[0]) if ts.shape[0] else 0

    def _advance(self):
        """Nächstes Fenster aller Ströme (oder None); Ströme enden unabhängig, fehlende Teile sind leer."""
        out, alive = {}, False
        for name, g in self._streams:
            try:
                k, *item = next(g)
                out[name] = item
                alive = True
            except StopIteration:
                out[name] = None
        return out if alive else None

    def _pull(self) -> bool:
        import time
        from ..io.session import split_events
        cur = self._next
        if cur is None:
            return False
        self._next = self._advance()
        last = self._next is None
        k = self._k
        self._k += 1
        run = self._run
        t1 = (k + 1) * self.chunk_s
        if last:
            t1 = max(k * self.chunk_s, min(t1, float(run.duration_s or t1)))
        if self.realtime:
            wait = self._t_wall0 + t1 / self.speed - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        if "harp" in self.kinds:
            raw_h = cur["harp"][0] if cur.get("harp") is not None else np.empty(0, np.uint32)
            hs = split_events(self._dec.decode(raw_h), run.channel_roles, run.marker_roles)
            her, mk = hs.role_s("herald"), hs.role_s("sync_marker")
            self._q["harp"].append(HarpData(t1, her, mk, len(her) + len(mk), last, records=raw_h))
        if "digitizer" not in self.kinds:
            return True
        tu = run.time_unit
        w = cur["rec"][0] if cur.get("rec") is not None else None
        rm = w.index if w is not None else np.empty(0, np.int64)
        mts, mb = cur["mk"] if cur.get("mk") is not None else (np.empty(0, np.int64), None)
        if len(rm):
            data = run.read_records(slice(int(rm[0]), int(rm[-1]) + 1), channel="A")
            db = run.read_records(slice(int(rm[0]), int(rm[-1]) + 1), channel="B") if run.has_data_b else None
        else:
            data = np.empty((0, run.record_length), np.int16)
            db = np.empty((0, int(run.device.get("channel_b_record_length", 0))), np.int16) if run.has_data_b else None
        ts = w.timestamp if w is not None else np.empty(0, np.int64)
        rs = w.record_start if w is not None else np.empty(0, np.int64)
        trs = w.trigger_source if w is not None else np.empty(0, np.uint8)
        aux = None
        if getattr(run, "has_ramp", False):
            aux = dict(ramp_raw=run.read_ramp_raw(slice(int(rm[0]), int(rm[-1]) + 1)) if len(rm)
                       else np.empty(0, np.int16))
        raw = dict(timestamp=ts, record_start=rs, trigger_source=trs, markers=mts, data_b=db, marker_b=mb, aux=aux)
        lo_b = db if db is not None else (mb if mb is not None and len(mb) else None)
        self._q["digitizer"].append(DigData(data, lo_b, ts.astype(float) * tu, (ts + rs).astype(float) * tu,
                                            mts.astype(float) * tu, rm.astype(np.uint32), last, raw=raw))
        return True

    def read(self, kind: str):
        while not self._q[kind]:
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


def make_sim_backend(engine: str, base_config=None, chunk_s: float = 0.1, speed: float = 1.0, realtime: bool = True,
                     time_scale: float | None = None) -> SimBackend:
    """``engine``: ``batch`` (ganze Sitzung im Speicher, ``hhd.sim.engine``) oder ``stream`` (Zeitscheiben,
    beschränkter Speicher, ``hhd.sim.streaming``)."""
    if engine not in SIM_ENGINES:
        raise DeviceError(f"sim_engine: {' | '.join(SIM_ENGINES)} (nicht {engine!r})")
    cls = StreamSimBackend if engine == "stream" else SimBackend
    return cls(base_config, chunk_s, speed, realtime, time_scale)


SIM_ENGINES = ("batch", "stream")


def make_devices(modes, base_config=None, replay_dir=None, chunk_s: float = 0.1, speed: float = 1.0,
                 realtime: bool = True, time_scale: float | None = None, live_cfg=None, sim_engine: str = "batch"):
    """(backend, harp, digitizer) für ``modes`` = {"harp": m, "digitizer": m} mit m ∈ {live, sim, replay}
    (eine Zeichenkette gilt für beide Geräte).

    Simulierte Geräte teilen sich einen Simulator (über SYNC-Marker gekoppelte Ströme), wiedergegebene eine
    Wiedergabe; im gemischten Betrieb liest der gemeinsame Hintergrund nur die Ströme seiner Geräte."""
    if isinstance(modes, str):
        modes = {"harp": modes, "digitizer": modes}
    for k, m in modes.items():
        if m not in ("live", "sim", "replay"):
            raise DeviceError(f"unbekannte Betriebsart {m!r} ({k})")
    hm, dm = modes["harp"], modes["digitizer"]
    if "sim" in (hm, dm) and "replay" in (hm, dm):
        raise DeviceError("sim und replay lassen sich nicht mischen (getrennte Zeitbasis); live mit sim oder replay ist möglich")
    be = None
    if "sim" in (hm, dm):
        be = make_sim_backend(sim_engine, base_config, chunk_s, speed, realtime, time_scale)
    elif "replay" in (hm, dm):
        if not replay_dir:
            raise DeviceError("replay: Verzeichnis der Sitzung fehlt (devices.replay_dir)")
        be = ReplayBackend(replay_dir, chunk_s, speed, realtime)
    if be is not None:
        be.kinds = {k for k, m in modes.items() if m != "live"}
    if "live" in (hm, dm):
        from .livedev import ADQ32Live, HydraHarp500Live, LiveCfg
        lc = live_cfg or LiveCfg()
    harp = (HydraHarp500Live(lc) if hm == "live" else SimHarp(be) if hm == "sim" else ReplayHarp(be))
    dig = (ADQ32Live(lc) if dm == "live" else SimDigitizer(be) if dm == "sim" else ReplayDigitizer(be))
    return be, harp, dig
