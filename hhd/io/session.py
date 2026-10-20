"""Import einer Messsitzung (AP3): Manifest, PTU-Dateien der HydraHarp 500 (Strom 1) und ADQ32-HDF5 (Strom 2).

Gelesen werden **nur** Dateien, die auch eine echte Messung liefert:

* ``session.json`` – davon nur die Struktur (Sitzungs-, Kampagnen-, Kalibrierungs-ID, Läufe mit Lauf-ID,
  Laufart, Rolle, Startzeit, Dauer, PTU-Datei, Prüfsummen). Die Felder ``expected``, ``config`` und ``seed``, die
  der Simulator zusätzlich schreibt, werden **nicht** übernommen: Sie enthalten simulierte Wahrheiten
  (Laufzeiten, Uhrparameter) und gibt es bei realen Messungen nicht.
* ``harp_<lauf>.ptu`` – PicoQuant Generic T2 (Kanalrollen aus dem PTU-Kopf ``UsrHHD_ChannelRoles``).
* ``digitizer.h5`` – HDF5-Schema 1.2 (Records Kanal A, ``records/data_b`` Kanal B, SYNC-Marker).

Die Wahrheitsdatei ``truth.h5`` des Simulators wird hier nie geöffnet (``FORBIDDEN_FILES``); nur die
Validierung (``hhd.sim.sync_validate``) und die Tests vergleichen gegen sie.

Großer Datenumfang: Wellenformen werden erst bei Bedarf und blockweise gelesen (:meth:`RunData.iter_records`);
PTU-Records lassen sich mit :meth:`RunData.iter_harp` in Blöcken dekodieren (Datenstrom-fähig).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import h5py
import numpy as np

from . import ptu as P
from . import schema as S

MANIFEST = "session.json"
CAMPAIGN = "campaign.json"
FORBIDDEN_FILES = ("truth.h5",)
RUN_KEYS = ("run_id", "id", "index", "kind", "dataset_type", "comparison_role", "t_start_s", "duration_s",
            "ptu_file", "n_records", "n_harp_records")
SESSION_KEYS = ("format", "schema_version", "software", "simulated", "session_id", "campaign_id", "optics",
                "device_modes", "acquisition_mode", "hardware", "streaming", "source_session")
HARP_RES = 1e-12


class ImportError_(RuntimeError):
    """Fehler beim Import (beschädigte oder unvollständige Sitzung)."""


def _guard(path: Path) -> Path:
    if path.name in FORBIDDEN_FILES:
        raise PermissionError(f"{path.name}: Wahrheitsdatei – für Import und Synchronisation gesperrt")
    return path


def _role_name(s: str) -> str:
    return str(s).split(" ", 1)[0].split("(", 1)[0].strip()


def _roles(js: str | None) -> dict[int, str]:
    if not js:
        return {}
    try:
        d = json.loads(js)
    except (TypeError, ValueError):
        return {}
    return {int(k): _role_name(v) for k, v in d.items()}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(_guard(path), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class HarpStream:
    """Ereignisse eines Laufs in Harp-Zeit (s seit Laufbeginn), nach Rolle getrennt."""
    channels: dict[int, np.ndarray]          # Kanal → Zeiten in ps (int64), Kanal 0 = Sync-Eingang
    markers: dict[int, np.ndarray]           # Marker-Bit → Zeiten in ps
    channel_roles: dict[int, str]
    marker_roles: dict[int, str]
    n_records: int = 0

    def role_ps(self, role: str) -> np.ndarray:
        for ch, r in self.channel_roles.items():
            if r == role and ch in self.channels:
                return self.channels[ch]
        for bit, r in self.marker_roles.items():
            if r == role and bit in self.markers:
                return self.markers[bit]
        return np.empty(0, np.int64)

    def role_s(self, role: str) -> np.ndarray:
        return self.role_ps(role).astype(np.float64) * HARP_RES

    def roles_present(self) -> list[str]:
        out = [r for ch, r in self.channel_roles.items() if len(self.channels.get(ch, ()))]
        out += [r for b, r in self.marker_roles.items() if len(self.markers.get(b, ()))]
        return out


def split_events(ev: P.T2Events, channel_roles: dict[int, str], marker_roles: dict[int, str]) -> HarpStream:
    ch = {}
    for c in np.unique(ev.event_channel):
        t = ev.event_time_ps[ev.event_channel == c]
        ch[int(c)] = np.sort(t)                                   # Marker können leicht ungeordnet sein
    mk = {}
    for bit in range(4):
        m = 1 << bit
        t = ev.marker_time_ps[(ev.marker_bits & m) != 0]
        if len(t):
            mk[m] = np.sort(t)
    return HarpStream(ch, mk, channel_roles, marker_roles)


@dataclass
class RunData:
    """Ein Messlauf: Metadaten, Harp-Strom (lazy), Digitizer-Header (sofort), Wellenformen (bei Bedarf)."""
    session: "SessionData"
    run_id: str
    block_id: str
    index: int
    kind: str
    dataset_type: str
    comparison_role: str
    t_start_s: float
    duration_s: float
    ptu_path: Path
    attrs: dict[str, Any]
    ptu_tags: dict[str, Any] = field(default_factory=dict)
    n_records_file: int = 0                   # Records laut digitizer.h5 (auch ohne geladene Köpfe)
    has_data_b: bool = False
    has_marker_b: bool = False                # Kanal B an den SYNC-Markern (live, channel_b_trigger = SYNC)
    has_ramp: bool = False                    # records/ramp_raw (Piezo-Rampenspannung, Schema 1.3)
    ramp_lsb_V: float = float("nan")
    _harp: HarpStream | None = None
    _hdr: dict | None = None                  # Digitizer-Köpfe (timestamp, record_start, …); lazy, freigebbar

    # ------------------------------------------------------------------ Köpfe (lazy)
    def _headers(self) -> dict:
        if self._hdr is None:
            with h5py.File(self.session.h5_path, "r") as f:
                g = f["blocks"][self.block_id]
                rec = g["records"]
                self._hdr = dict(timestamp=rec["timestamp"][:].astype(np.int64),
                                 record_start=rec["record_start"][:].astype(np.int64),
                                 record_number=rec["record_number"][:], trigger_source=rec["trigger_source"][:],
                                 marker_ts=g["markers/timestamp"][:].astype(np.int64))
        return self._hdr

    timestamp = property(lambda self: self._headers()["timestamp"])
    record_start = property(lambda self: self._headers()["record_start"])
    record_number = property(lambda self: self._headers()["record_number"])
    trigger_source = property(lambda self: self._headers()["trigger_source"])
    marker_ts = property(lambda self: self._headers()["marker_ts"])

    def release(self) -> None:
        """Zwischengespeicherte Harp-Ereignisse und Digitizer-Köpfe freigeben (werden bei Bedarf neu gelesen).
        Lange Sitzungen: nach jedem Lauf aufrufen, dann wächst der Speicher nicht mit der Sitzungsdauer."""
        self._harp = None
        self._hdr = None

    def iter_headers(self, chunk: int = 1 << 16) -> Iterator[tuple[int, dict]]:
        """Digitizer-Köpfe blockweise (ohne den Zwischenspeicher zu füllen)."""
        with h5py.File(self.session.h5_path, "r") as f:
            rec = f["blocks"][self.block_id]["records"]
            for i0 in range(0, self.n_records, chunk):
                yield i0, dict(timestamp=rec["timestamp"][i0:i0 + chunk].astype(np.int64),
                               record_start=rec["record_start"][i0:i0 + chunk].astype(np.int64),
                               trigger_source=rec["trigger_source"][i0:i0 + chunk])

    # ------------------------------------------------------------------ Strom 1
    @property
    def channel_roles(self) -> dict[int, str]:
        return _roles(self.ptu_tags.get("UsrHHD_ChannelRoles") or self.attrs.get("harp_channel_roles"))

    @property
    def marker_roles(self) -> dict[int, str]:
        return _roles(self.ptu_tags.get("UsrHHD_MarkerRoles") or self.attrs.get("harp_marker_roles"))

    @property
    def harp(self) -> HarpStream:
        if self._harp is None:
            dec = P.T2Decoder()
            parts = [dec.decode(r) for r in P.iter_ptu_records(self.ptu_path)]
            ev = P.T2Events(*(np.concatenate([getattr(p, f) for p in parts]) if parts else np.empty(0, np.int64)
                              for f in ("event_time_ps", "event_channel", "marker_time_ps", "marker_bits")))
            self._harp = split_events(ev, self.channel_roles, self.marker_roles)
            self._harp.n_records = dec.n_records
        return self._harp

    def iter_harp(self, chunk_records: int = 1 << 18) -> Iterator[HarpStream]:
        """Harp-Strom in Blöcken (Datenstrom-Modus); ein Decoder für Überläufe über Blockgrenzen."""
        dec = P.T2Decoder()
        cr, mr = self.channel_roles, self.marker_roles
        for rec in P.iter_ptu_records(self.ptu_path, chunk_records):
            hs = split_events(dec.decode(rec), cr, mr)
            hs.n_records = len(rec)
            yield hs

    # ------------------------------------------------------------------ Strom 2
    @property
    def device(self) -> dict[str, Any]:
        return self.session.device

    @property
    def time_unit(self) -> float:
        return float(self.device["time_unit_s"])

    @property
    def fs(self) -> float:
        return float(self.device["sampling_rate_Hz"])

    @property
    def record_length(self) -> int:
        return int(self.device["record_length"])

    @property
    def n_records(self) -> int:
        return self.n_records_file

    def trigger_D(self) -> np.ndarray:
        """Trigger-Zeitpunkte in Digitizer-Zeit (s)."""
        return self.timestamp.astype(np.float64) * self.time_unit

    def first_sample_D(self) -> np.ndarray:
        return (self.timestamp + self.record_start).astype(np.float64) * self.time_unit

    def markers_D(self) -> np.ndarray:
        return np.sort(self.marker_ts).astype(np.float64) * self.time_unit

    def read_records(self, idx: np.ndarray | slice | None = None, channel: str = "A",
                     cols: slice | None = None) -> np.ndarray:
        name = "data" if channel == "A" else "data_b"
        with h5py.File(self.session.h5_path, "r") as f:
            ds = f["blocks"][self.block_id]["records"][name]
            if idx is None:
                idx = slice(None)
            if isinstance(idx, np.ndarray):
                idx = np.sort(np.asarray(idx, np.int64))
                if len(idx) == 0:
                    return np.empty((0, ds.shape[1]), np.int16)
                out = np.empty((len(idx), ds.shape[1] if cols is None else len(range(*cols.indices(ds.shape[1])))), np.int16)
                # zusammenhängende Läufe blockweise lesen (h5py-Fancy-Indexing ist langsam)
                brk = np.flatnonzero(np.diff(idx) > 64) + 1
                pos = 0
                for part in np.split(idx, brk):
                    blk = ds[int(part[0]):int(part[-1]) + 1] if cols is None else ds[int(part[0]):int(part[-1]) + 1, cols]
                    out[pos:pos + len(part)] = blk[part - part[0]]
                    pos += len(part)
                return out
            return ds[idx] if cols is None else ds[idx, cols]

    def read_ramp(self, idx: slice | None = None) -> np.ndarray:
        """Rampenspannung je Record in V (Hilfs-ADC, ``records/ramp_raw`` × lsb_V); leer ohne Rampenspur."""
        if not self.has_ramp:
            return np.empty(0)
        with h5py.File(self.session.h5_path, "r") as f:
            ds = f["blocks"][self.block_id]["records"]["ramp_raw"]
            return ds[idx if idx is not None else slice(None)].astype(np.float64) * self.ramp_lsb_V

    def read_ramp_raw(self, idx: slice | None = None) -> np.ndarray:
        with h5py.File(self.session.h5_path, "r") as f:
            return f["blocks"][self.block_id]["records"]["ramp_raw"][idx if idx is not None else slice(None)]

    def iter_records(self, chunk: int = 1024, channel: str = "A") -> Iterator[tuple[int, np.ndarray]]:
        name = "data" if channel == "A" else "data_b"
        with h5py.File(self.session.h5_path, "r") as f:
            ds = f["blocks"][self.block_id]["records"][name]
            for i0 in range(0, ds.shape[0], chunk):
                yield i0, ds[i0:i0 + chunk]

    def marker_b_means(self) -> tuple[np.ndarray, np.ndarray]:
        """(Markerzeiten D in s, Mittelwert Kanal B je Marker) – sortiert nach Zeit; leer ohne ``markers/data_b``."""
        if not self.has_marker_b:
            return np.empty(0), np.empty(0)
        with h5py.File(self.session.h5_path, "r") as f:
            ds = f["blocks"][self.block_id]["markers"]["data_b"]
            m = np.empty(ds.shape[0])
            for i0 in range(0, ds.shape[0], 8192):
                m[i0:i0 + 8192] = ds[i0:i0 + 8192].mean(axis=1)
        t = self.marker_ts.astype(np.float64) * self.time_unit
        o = np.argsort(t, kind="stable")
        return t[o], m[o]

    def summary(self) -> dict[str, Any]:
        h = self.harp
        return dict(run_id=self.run_id, block=self.block_id, kind=self.kind, dataset_type=self.dataset_type,
                    duration_s=self.duration_s, n_records=self.n_records, n_sync_D=int(len(self.marker_ts)),
                    n_harp_records=int(h.n_records),
                    harp={r: int(len(h.role_ps(r))) for r in sorted(set(h.roles_present()))},
                    channel_b=self.has_data_b or self.has_marker_b)


@dataclass
class SessionData:
    path: Path
    manifest: dict[str, Any]
    device: dict[str, Any]
    root_attrs: dict[str, Any]
    runs: list[RunData]
    warnings: list[str] = field(default_factory=list)

    @property
    def h5_path(self) -> Path:
        return _guard(self.path / "digitizer.h5")

    @property
    def session_id(self) -> str:
        return str(self.manifest.get("session_id", ""))

    @property
    def campaign_id(self) -> str:
        return str(self.manifest.get("campaign_id", ""))

    @property
    def calibration_id(self) -> str:
        return str(self.manifest.get("calibration", {}).get("calibration_id", self.root_attrs.get("calibration_id", "")))

    def run(self, key: str) -> RunData:
        for r in self.runs:
            if key in (r.run_id, r.block_id):
                return r
        raise KeyError(key)

    def runs_of(self, *dataset_types: str) -> list[RunData]:
        return [r for r in self.runs if r.dataset_type in dataset_types]

    def summary(self) -> dict[str, Any]:
        return dict(session_id=self.session_id, campaign_id=self.campaign_id, calibration_id=self.calibration_id,
                    simulated=bool(self.manifest.get("simulated", False)), device=str(self.device.get("model", "")),
                    reference_clock=str(self.device.get("reference_clock", "")),
                    runs=[r.summary() for r in self.runs], warnings=self.warnings)


def _clean(v):
    if isinstance(v, (np.generic,)):
        return v.item()
    if isinstance(v, bytes):
        return v.decode()
    return v


def load_session(path: str | Path, verify: bool = True, lazy: bool = False) -> SessionData:
    """Sitzung importieren (Manifest + Digitizer-Header + PTU-Köpfe; Wellenformen und Harp-Ereignisse lazy).

    ``verify``: SHA-256 der gelesenen Dateien gegen das Manifest prüfen (Befunde in ``warnings``).
    ``lazy``: auch die Digitizer-Köpfe erst bei Bedarf lesen (lange Sitzungen; mit ``RunData.release``)."""
    d = Path(path)
    mpath = d / MANIFEST
    if not mpath.exists():
        raise ImportError_(f"{mpath}: Manifest fehlt")
    raw = json.loads(mpath.read_text(encoding="utf-8"))
    if raw.get("format") != "hhd-session":
        raise ImportError_(f"{mpath}: format != hhd-session")
    man = {k: raw[k] for k in SESSION_KEYS if k in raw}
    man["calibration"] = {k: v for k, v in raw.get("calibration", {}).items()
                          if k in ("calibration_id", "mode", "vacuum_runs")}
    man["runs"] = [{k: r[k] for k in RUN_KEYS if k in r} for r in raw.get("runs", [])]
    man["files"] = {k: v for k, v in raw.get("files", {}).items() if k not in FORBIDDEN_FILES}
    warnings: list[str] = []
    h5p = _guard(d / "digitizer.h5")
    if not h5p.exists():
        raise ImportError_(f"{h5p}: Digitizer-Datei fehlt")
    errs = S.validate_digitizer_file(h5p)
    warnings += [f"Schema: {e}" for e in errs]
    if verify:
        for name, info in man["files"].items():
            p = d / name
            if not p.exists():
                warnings.append(f"{name}: Datei fehlt")
            elif info.get("sha256") and sha256(p) != info["sha256"]:
                warnings.append(f"{name}: SHA-256 weicht vom Manifest ab")
    sess = SessionData(d, man, {}, {}, [], warnings)
    with h5py.File(h5p, "r") as f:
        sess.device = {k: _clean(v) for k, v in f["device"].attrs.items()}
        sess.root_attrs = {k: _clean(v) for k, v in f.attrs.items()}
        blocks = f.get("blocks", {})
        for r in man["runs"]:
            bid = r["id"]
            if bid not in blocks:
                warnings.append(f"{r['run_id']}: Gruppe /blocks/{bid} fehlt")
                continue
            g = blocks[bid]
            rec = g["records"]
            ptu = d / r.get("ptu_file", f"harp_{bid}.ptu")
            tags = {}
            if ptu.exists():
                try:
                    tags, _ = P.read_ptu_header(ptu)
                except P.PtuError as e:
                    warnings.append(f"{ptu.name}: {e}")
            else:
                warnings.append(f"{ptu.name}: PTU-Datei fehlt")
            rid = str(tags.get("UsrHHD_RunId", r["run_id"]))
            if rid != r["run_id"]:
                warnings.append(f"{ptu.name}: UsrHHD_RunId {rid} != Manifest {r['run_id']}")
            if _clean(g.attrs.get("run_id")) != r["run_id"]:
                warnings.append(f"/blocks/{bid}: run_id weicht vom Manifest ab")
            run = RunData(sess, r["run_id"], bid, int(r.get("index", 0)), r.get("kind", ""), r.get("dataset_type", ""),
                          r.get("comparison_role", ""), float(r.get("t_start_s", 0.0)), float(r.get("duration_s", 0.0)),
                          ptu, {k: _clean(v) for k, v in g.attrs.items()}, tags, int(rec["timestamp"].shape[0]),
                          "data_b" in rec)
            run.has_marker_b = "data_b" in g["markers"]
            if "ramp_raw" in rec:
                run.has_ramp = True
                run.ramp_lsb_V = float(_clean(rec["ramp_raw"].attrs.get("lsb_V", run.attrs.get("phase_ramp_lsb_V", np.nan))))
            if not lazy:
                run._hdr = dict(timestamp=rec["timestamp"][:].astype(np.int64),
                                record_start=rec["record_start"][:].astype(np.int64),
                                record_number=rec["record_number"][:], trigger_source=rec["trigger_source"][:],
                                marker_ts=g["markers/timestamp"][:].astype(np.int64))
            sess.runs.append(run)
    return sess


@dataclass
class CampaignData:
    path: Path
    campaign_id: str
    sessions: list[SessionData]
    warnings: list[str] = field(default_factory=list)


def load_campaign(path: str | Path, verify: bool = True) -> CampaignData:
    d = Path(path)
    raw = json.loads((d / CAMPAIGN).read_text(encoding="utf-8"))
    if raw.get("format") != "hhd-campaign":
        raise ImportError_(f"{d / CAMPAIGN}: format != hhd-campaign")
    warn, sess = [], []
    for s in raw.get("sessions", []):
        sd = d / s["dir"]
        if verify and s.get("manifest_sha256") and sha256(sd / MANIFEST) != s["manifest_sha256"]:
            warn.append(f"{s['dir']}: Manifest-Prüfsumme weicht ab")
        sess.append(load_session(sd, verify=verify))
    return CampaignData(d, str(raw.get("campaign_id", "")), sess, warn)


def is_campaign(path: str | Path) -> bool:
    return (Path(path) / CAMPAIGN).exists()
