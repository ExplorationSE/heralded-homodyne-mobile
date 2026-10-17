"""HDF5-Schema v1.2 für Digitizer-Daten (Strom 2) und Wahrheitsdateien des Simulators.

Das Schema ist ein **[Vorschlag]** (Gesamtplan, Kap. 6 „Datenformate“), angelehnt an die Record-Header des
ADQ3 User Guide 21-2539, Kap. 9.3: ``timestamp`` und ``record_start`` als ganze Zahlen in ``time_unit``,
Zeit des ersten Samples ``t_x[0] = timestamp + record_start`` **[Spez.]**.

Aufbau ``digitizer.h5``::

    /                      attrs: format="hhd-digitizer", schema_version="1.2", simulated, software, created,
                                  session_id, campaign_id, calibration_id
    /device                attrs: model, form_factor, clock_option, channel_mode, f_adc_Hz, skip_factor,
                                  sampling_rate_Hz, time_unit_s, sampling_period_tu, adc_bits, record_length,
                                  horizontal_offset, trig_resolution_s, sync_resolution_s, reference_clock,
                                  header_reports_trigger_source, bandwidth_option, analog_bandwidth_Hz,
                                  channel_a_role, channel_b_role, channel_b_skip_factor, channel_b_record_length,
                                  channel_b_horizontal_offset, lo_monitor_dark_lsb, lo_monitor_lsb_nominal
    /blocks/<block_id>     ein Messlauf; attrs: run_id, session_id, campaign_id, comparison_role
                                  (heralded | unheralded | control | test_state), t_start_s (seit Sitzungsbeginn),
                                  dataset_type, kind, sequence_index, calibration_id (= Sitzung), signal_blocked,
                                  lo_on, phase_mode, phase_theta0_rad, phase_ramp_Hz, phase_steps_rad,
                                  phase_dwell_s, ptu_file, duration_s, harp_channel_roles, harp_marker_roles,
                                  adq32_trigger_source
        records/data            int16 (n, L)   ADC-Werte Kanal A (Homodyn) in LSB
        records/data_b          int16 (n, L_B) optional: Kanal B (LO-Monitor, Photodiode), gleicher Trigger;
                                               L_B = channel_b_record_length (eigener Sample Skip)
        records/timestamp       int64 (n)      Trigger-Zeitpunkt in time_unit (Zähler seit Start)
        records/record_start    int64 (n)      erstes Sample relativ zu timestamp (≤ 0 bei Pretrigger)
        records/record_number   uint32 (n)
        records/trigger_source  uint8 (n)      TRIGGER_SOURCES; 255 = vom Header nicht gemeldet
        markers/timestamp       int64 (m)      SYNC-Ereignisse in time_unit
        markers/data_b          int16 (m, L_B) optional (live, ``channel_b_trigger`` = "SYNC"): Kanal B (LO-Monitor)
                                               zu den Markerzeiten; dann fehlt records/data_b (Kanal B wird vom
                                               SYNC-Port getriggert und liefert so die Markerzeiten, ED-7)
        monitor/t_s, monitor/lo_power_rel      optional: externer LO-Leistungsmesser (relativ)
    /calibration           attrs: calibration_id, mode ("shared"), runs, vacuum_blocks (JSON-Liste), electronic_blocks

v1.0 → v1.1 (additiv): Sitzungs-/Lauf-IDs, dataset_type ``free_running``, Gruppe ``monitor``.
v1.1 → v1.2 (additiv): Kanal B ``records/data_b`` (LO-Monitor), Kanalrollen von Harp und ADQ32.
1.2 (additiv, ohne Versionswechsel): ``markers/data_b`` und Geräteattribut ``channel_b_trigger``; Wurzelattribute
``device_mode_harp``, ``device_mode_digitizer``, ``acquisition_mode`` (Betriebsart je Gerät).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import h5py
import numpy as np

SCHEMA_VERSION = "1.2"         # 1.2: Kanal B (LO-Monitor), Kanalrollen (Gesamtplan v1.5)
FORMAT_DIGITIZER = "hhd-digitizer"
FORMAT_TRUTH = "hhd-truth"

DATASET_TYPES = (
    "heralded",          # herald-getriggerte Records
    "random_trigger",    # Zufallstrigger (eigener Lauf oder eingestreut)
    "free_running",      # freilaufender (periodischer) Trigger, Signal offen: Lauf ohne Herald
    "vacuum",            # K-V: Signal blockiert, LO an
    "herald_blocked",    # K-H: Signal blockiert, Herald triggert
    "electronic",        # LO aus: nur elektronisches Rauschen
    "prepared_state",    # getriggerter Testzustand (kohärent, thermisch, Fock, Katze)
)

TRIGGER_SOURCES = {"TRIG": 0, "SYNC": 1, "GPIO": 2, "PERIODIC": 3, "SOFTWARE": 4, "UNKNOWN": 255}

REQ_DEVICE_ATTRS = ("model", "form_factor", "clock_option", "channel_mode", "f_adc_Hz", "skip_factor",
                    "sampling_rate_Hz", "time_unit_s", "sampling_period_tu", "adc_bits", "record_length",
                    "horizontal_offset")
REQ_ROOT_ATTRS = ("session_id", "campaign_id", "calibration_id")
REQ_BLOCK_ATTRS = ("dataset_type", "kind", "sequence_index", "calibration_id", "signal_blocked", "lo_on",
                   "phase_mode", "run_id", "session_id", "comparison_role", "t_start_s")
REQ_RECORD_SETS = {"data": np.int16, "timestamp": np.int64, "record_start": np.int64,
                   "record_number": np.uint32, "trigger_source": np.uint8}


def _attrs(obj, d: dict[str, Any]) -> None:
    for k, v in d.items():
        if isinstance(v, (list, tuple, dict)):
            v = json.dumps(v)
        elif v is None:
            v = ""
        obj.attrs[k] = v


def write_block(f: h5py.File, block_id: str, attrs: dict[str, Any], data: np.ndarray, timestamp: np.ndarray,
                record_start: np.ndarray, trigger_source: np.ndarray, marker_ts: np.ndarray,
                monitor: dict | None = None, data_b: np.ndarray | None = None) -> None:
    g = f.require_group("blocks").create_group(block_id)
    _attrs(g, attrs)
    r = g.create_group("records")
    n, L = data.shape if data.ndim == 2 else (0, int(f["device"].attrs["record_length"]))
    data = data.reshape(n, L).astype(np.int16)
    r.create_dataset("data", data=data, chunks=(max(1, min(256, n)), L) if n else None,
                     compression="gzip" if n else None, compression_opts=4 if n else None)
    r.create_dataset("timestamp", data=np.asarray(timestamp, np.int64))
    r.create_dataset("record_start", data=np.asarray(record_start, np.int64))
    r.create_dataset("record_number", data=np.arange(n, dtype=np.uint32))
    r.create_dataset("trigger_source", data=np.asarray(trigger_source, np.uint8))
    r["data"].attrs["units"] = "ADC LSB"
    r["data"].attrs["channel"] = "A"
    if data_b is not None:                                   # Kanal B: LO-Monitor
        Lb = int(f["device"].attrs.get("channel_b_record_length", data_b.shape[1] if data_b.ndim == 2 else 0))
        db = np.asarray(data_b, np.int16).reshape(n, Lb)
        r.create_dataset("data_b", data=db, chunks=(max(1, min(1024, n)), Lb) if n and Lb else None,
                         compression="gzip" if n else None, compression_opts=4 if n else None)
        r["data_b"].attrs.update(units="ADC LSB", channel="B", role=str(f["device"].attrs.get("channel_b_role", "")))
    r["timestamp"].attrs["units"] = "time_unit"
    r["record_start"].attrs["units"] = "time_unit"
    m = g.create_group("markers")
    m.create_dataset("timestamp", data=np.asarray(marker_ts, np.int64))
    m["timestamp"].attrs["units"] = "time_unit"
    if monitor is not None:                                  # LO-Leistungsmonitor (optional)
        mo = g.create_group("monitor")
        mo.create_dataset("t_s", data=np.asarray(monitor["t_s"], float)).attrs["units"] = "s seit Laufbeginn"
        mo.create_dataset("lo_power_rel", data=np.asarray(monitor["lo_power_rel"], float)).attrs["units"] = "relativ"


def open_new(path: str | Path, root_attrs: dict[str, Any], device_attrs: dict[str, Any]) -> h5py.File:
    f = h5py.File(path, "w")
    _attrs(f, dict(format=FORMAT_DIGITIZER, schema_version=SCHEMA_VERSION, **root_attrs))
    _attrs(f.create_group("device"), device_attrs)
    return f


def _s(v) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


def validate_digitizer_file(path: str | Path) -> list[str]:
    """Prüft eine Digitizer-Datei gegen Schema v1. Rückgabe: Liste der Befunde (leer = gültig)."""
    err: list[str] = []
    with h5py.File(path, "r") as f:
        if f.attrs.get("format") != FORMAT_DIGITIZER:
            err.append(f"/: format != {FORMAT_DIGITIZER!r}")
        if f.attrs.get("schema_version") != SCHEMA_VERSION:
            err.append(f"/: schema_version != {SCHEMA_VERSION!r}")
        for a in REQ_ROOT_ATTRS:
            if a not in f.attrs:
                err.append(f"/: Attribut {a} fehlt")
        if "device" not in f:
            return err + ["/device fehlt"]
        dev = f["device"].attrs
        for a in REQ_DEVICE_ATTRS:
            if a not in dev:
                err.append(f"/device: Attribut {a} fehlt")
        L = int(dev.get("record_length", -1))
        for bid, g in f.get("blocks", {}).items():
            for a in REQ_BLOCK_ATTRS:
                if a not in g.attrs:
                    err.append(f"/blocks/{bid}: Attribut {a} fehlt")
            dt = g.attrs.get("dataset_type")
            if dt not in DATASET_TYPES:
                err.append(f"/blocks/{bid}: dataset_type {dt!r} unbekannt")
            if "records" not in g or "markers" not in g:
                err.append(f"/blocks/{bid}: records/markers fehlen")
                continue
            r = g["records"]
            n = None
            for name, typ in REQ_RECORD_SETS.items():
                if name not in r:
                    err.append(f"/blocks/{bid}/records/{name} fehlt")
                    continue
                if r[name].dtype != np.dtype(typ):
                    err.append(f"/blocks/{bid}/records/{name}: dtype {r[name].dtype} != {np.dtype(typ)}")
                k = r[name].shape[0]
                n = k if n is None else n
                if k != n:
                    err.append(f"/blocks/{bid}/records/{name}: Länge {k} != {n}")
            if "data" in r and r["data"].ndim == 2 and r["data"].shape[1] != L:
                err.append(f"/blocks/{bid}/records/data: Recordlänge {r['data'].shape[1]} != {L}")
            if "data_b" in r:
                Lb = int(dev.get("channel_b_record_length", -1))
                if r["data_b"].dtype != np.dtype(np.int16):
                    err.append(f"/blocks/{bid}/records/data_b: dtype {r['data_b'].dtype} != int16")
                if n is not None and r["data_b"].shape[0] != n:
                    err.append(f"/blocks/{bid}/records/data_b: Länge {r['data_b'].shape[0]} != {n}")
                if r["data_b"].ndim != 2 or r["data_b"].shape[1] != Lb:
                    err.append(f"/blocks/{bid}/records/data_b: Recordlänge != channel_b_record_length {Lb}")
            elif dev.get("channel_b_role", "off") not in ("off", b"off") and _s(dev.get("channel_b_trigger", "")) == "SYNC":
                mb = g["markers"].get("data_b")
                if mb is None:
                    err.append(f"/blocks/{bid}/markers/data_b fehlt (channel_b_trigger = SYNC)")
                elif mb.shape[0] != g["markers"]["timestamp"].shape[0] or mb.dtype != np.dtype(np.int16):
                    err.append(f"/blocks/{bid}/markers/data_b: Länge/dtype passt nicht zu markers/timestamp")
            elif dev.get("channel_b_role", "off") not in ("off", b"off"):
                err.append(f"/blocks/{bid}/records/data_b fehlt (channel_b_role = {dev.get('channel_b_role')})")
            if "timestamp" in r and r["timestamp"].shape[0] > 1 and np.any(np.diff(r["timestamp"][:]) <= 0):
                err.append(f"/blocks/{bid}/records/timestamp nicht streng monoton")
            if "timestamp" not in g["markers"]:
                err.append(f"/blocks/{bid}/markers/timestamp fehlt")
            if g.attrs.get("calibration_id") != f.attrs.get("calibration_id"):
                err.append(f"/blocks/{bid}: calibration_id weicht von der Sitzung ab")
        rids = [g.attrs.get("run_id") for g in f.get("blocks", {}).values()]
        if len(set(rids)) != len(rids):
            err.append("/blocks: run_id doppelt")
    return err


def read_block(path: str | Path, block_id: str) -> dict[str, Any]:
    with h5py.File(path, "r") as f:
        g = f["blocks"][block_id]
        out = {k: g["records"][k][:] for k in REQ_RECORD_SETS}
        out["marker_timestamp"] = g["markers/timestamp"][:]
        out["attrs"] = dict(g.attrs)
        out["device"] = dict(f["device"].attrs)
    return out
