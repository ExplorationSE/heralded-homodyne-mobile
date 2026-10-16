"""HDF5-Schema v1 für Digitizer-Daten (Strom 2) und Wahrheitsdateien des Simulators.

Das Schema ist ein **[Vorschlag]** (Gesamtplan, Kap. 6 „Datenformate“), angelehnt an die Record-Header des
ADQ3 User Guide 21-2539, Kap. 9.3: ``timestamp`` und ``record_start`` als ganze Zahlen in ``time_unit``,
Zeit des ersten Samples ``t_x[0] = timestamp + record_start`` **[Spez.]**.

Aufbau ``digitizer.h5``::

    /                      attrs: format="hhd-digitizer", schema_version="1.0", simulated, software, created, run_name
    /device                attrs: model, form_factor, clock_option, channel_mode, f_adc_Hz, skip_factor,
                                  sampling_rate_Hz, time_unit_s, sampling_period_tu, adc_bits, record_length,
                                  horizontal_offset, trig_resolution_s, sync_resolution_s, reference_clock,
                                  header_reports_trigger_source
    /blocks/<block_id>     attrs: dataset_type, kind, sequence_index, calibration_id, signal_blocked, lo_on,
                                  phase_mode, phase_theta0_rad, phase_ramp_Hz, phase_steps_rad, phase_dwell_s,
                                  ptu_file, duration_s
        records/data            int16 (n, L)   ADC-Werte in LSB
        records/timestamp       int64 (n)      Trigger-Zeitpunkt in time_unit (Zähler seit Start)
        records/record_start    int64 (n)      erstes Sample relativ zu timestamp (≤ 0 bei Pretrigger)
        records/record_number   uint32 (n)
        records/trigger_source  uint8 (n)      TRIGGER_SOURCES; 255 = vom Header nicht gemeldet
        markers/timestamp       int64 (m)      SYNC-Ereignisse in time_unit
    /calibration           attrs: calibration_id, vacuum_blocks (JSON-Liste), electronic_blocks
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import h5py
import numpy as np

SCHEMA_VERSION = "1.0"
FORMAT_DIGITIZER = "hhd-digitizer"
FORMAT_TRUTH = "hhd-truth"

DATASET_TYPES = (
    "heralded",          # herald-getriggerte Records
    "random_trigger",    # Zufallstrigger (eigener Block oder eingestreut)
    "vacuum",            # K-V: Signal blockiert, LO an
    "herald_blocked",    # K-H: Signal blockiert, Herald triggert
    "electronic",        # LO aus: nur elektronisches Rauschen
    "prepared_state",    # getriggerter Testzustand (kohärent, thermisch, Fock, Katze)
)

TRIGGER_SOURCES = {"TRIG": 0, "SYNC": 1, "GPIO": 2, "PERIODIC": 3, "SOFTWARE": 4, "UNKNOWN": 255}

REQ_DEVICE_ATTRS = ("model", "form_factor", "clock_option", "channel_mode", "f_adc_Hz", "skip_factor",
                    "sampling_rate_Hz", "time_unit_s", "sampling_period_tu", "adc_bits", "record_length",
                    "horizontal_offset")
REQ_BLOCK_ATTRS = ("dataset_type", "kind", "sequence_index", "calibration_id", "signal_blocked", "lo_on",
                   "phase_mode")
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
                record_start: np.ndarray, trigger_source: np.ndarray, marker_ts: np.ndarray) -> None:
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
    r["timestamp"].attrs["units"] = "time_unit"
    r["record_start"].attrs["units"] = "time_unit"
    m = g.create_group("markers")
    m.create_dataset("timestamp", data=np.asarray(marker_ts, np.int64))
    m["timestamp"].attrs["units"] = "time_unit"


def open_new(path: str | Path, root_attrs: dict[str, Any], device_attrs: dict[str, Any]) -> h5py.File:
    f = h5py.File(path, "w")
    _attrs(f, dict(format=FORMAT_DIGITIZER, schema_version=SCHEMA_VERSION, **root_attrs))
    _attrs(f.create_group("device"), device_attrs)
    return f


def validate_digitizer_file(path: str | Path) -> list[str]:
    """Prüft eine Digitizer-Datei gegen Schema v1. Rückgabe: Liste der Befunde (leer = gültig)."""
    err: list[str] = []
    with h5py.File(path, "r") as f:
        if f.attrs.get("format") != FORMAT_DIGITIZER:
            err.append(f"/: format != {FORMAT_DIGITIZER!r}")
        if f.attrs.get("schema_version") != SCHEMA_VERSION:
            err.append(f"/: schema_version != {SCHEMA_VERSION!r}")
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
            if "timestamp" in r and r["timestamp"].shape[0] > 1 and np.any(np.diff(r["timestamp"][:]) <= 0):
                err.append(f"/blocks/{bid}/records/timestamp nicht streng monoton")
            if "timestamp" not in g["markers"]:
                err.append(f"/blocks/{bid}/markers/timestamp fehlt")
    return err


def read_block(path: str | Path, block_id: str) -> dict[str, Any]:
    with h5py.File(path, "r") as f:
        g = f["blocks"][block_id]
        out = {k: g["records"][k][:] for k in REQ_RECORD_SETS}
        out["marker_timestamp"] = g["markers/timestamp"][:]
        out["attrs"] = dict(g.attrs)
        out["device"] = dict(f["device"].attrs)
    return out
