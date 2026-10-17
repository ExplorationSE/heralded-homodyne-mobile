"""Schreiben im Datenstrom (Live- und gemischter Betrieb): PTU-Datei der HydraHarp und ``digitizer.h5`` (Schema 1.2)
wachsen während des Laufs; der Speicherbedarf hängt nicht von der Laufdauer ab.

* :class:`PtuStreamWriter` – Kopf mit Platzhalter für ``TTResult_NumberOfRecords``, Records werden angehängt, die
  Anzahl wird beim Schließen eingetragen (eine unvollständig geschlossene Datei hat Anzahl 0 → beim Lesen als leer
  erkannt, Rohdaten bleiben erhalten).
* :class:`DigitizerStreamWriter` – eine Gruppe je Lauf mit erweiterbaren, gzip-komprimierten Datensätzen; nach jedem
  Lauf ``flush``.
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from . import ptu as P
from . import schema as S


class PtuStreamWriter:
    def __init__(self, path: str | Path, **header_kw):
        self.path = Path(path)
        hdr = P.build_header(0, **header_kw)
        key = b"TTResult_NumberOfRecords".ljust(32, b"\0")
        i = hdr.find(key)
        if i < 0:
            raise RuntimeError("PTU-Kopf ohne TTResult_NumberOfRecords")
        self._count_pos = i + 32 + 8                    # Name (32) + Index/Typ (8) → Wert (int64)
        self._fh = open(self.path, "wb")
        self._fh.write(hdr)
        self.n = 0

    def write(self, records: np.ndarray) -> None:
        r = np.asarray(records, dtype="<u4")
        if len(r):
            self._fh.write(r.tobytes())
            self.n += len(r)

    def close(self) -> int:
        if self._fh is None:
            return self.n
        self._fh.seek(self._count_pos)
        self._fh.write(struct.pack("<q", self.n))
        self._fh.close()
        self._fh = None
        return self.n


def _ext(ds: h5py.Dataset, arr: np.ndarray) -> None:
    if len(arr) == 0:
        return
    n = ds.shape[0]
    ds.resize(n + len(arr), axis=0)
    ds[n:] = arr


class DigitizerStreamWriter:
    def __init__(self, path: str | Path, root_attrs: dict[str, Any], device_attrs: dict[str, Any]):
        self.path = Path(path)
        self.f = S.open_new(self.path, root_attrs, device_attrs)
        self.g = None
        self.block_ids: list[str] = []
        self.counts: dict[str, int] = {}

    def update_device(self, attrs: dict[str, Any]) -> None:
        S._attrs(self.f["device"], attrs)

    def begin_block(self, block_id: str, attrs: dict[str, Any]) -> None:
        dev = self.f["device"].attrs
        L = int(dev["record_length"])
        Lb = int(dev.get("channel_b_record_length", 0))
        role_b = S._s(dev.get("channel_b_role", "off"))
        trig_b = S._s(dev.get("channel_b_trigger", ""))
        g = self.f.require_group("blocks").create_group(block_id)
        S._attrs(g, attrs)
        r = g.create_group("records")
        kw = dict(compression="gzip", compression_opts=4)
        r.create_dataset("data", (0, L), np.int16, maxshape=(None, L), chunks=(256, L), **kw)
        r["data"].attrs.update(units="ADC LSB", channel="A")
        for name, typ in (("timestamp", np.int64), ("record_start", np.int64), ("record_number", np.uint32),
                          ("trigger_source", np.uint8)):
            r.create_dataset(name, (0,), typ, maxshape=(None,), chunks=(4096,))
        r["timestamp"].attrs["units"] = "time_unit"
        r["record_start"].attrs["units"] = "time_unit"
        m = g.create_group("markers")
        m.create_dataset("timestamp", (0,), np.int64, maxshape=(None,), chunks=(4096,)).attrs["units"] = "time_unit"
        self.b_mode = None
        if role_b != "off" and Lb > 0:
            if trig_b == "SYNC":
                m.create_dataset("data_b", (0, Lb), np.int16, maxshape=(None, Lb), chunks=(1024, Lb), **kw)
                m["data_b"].attrs.update(units="ADC LSB", channel="B", role=role_b, trigger="SYNC")
                self.b_mode = "markers"
            else:
                r.create_dataset("data_b", (0, Lb), np.int16, maxshape=(None, Lb), chunks=(1024, Lb), **kw)
                r["data_b"].attrs.update(units="ADC LSB", channel="B", role=role_b)
                self.b_mode = "records"
        self.g, self.bid, self.n = g, block_id, 0
        self.block_ids.append(block_id)

    def append(self, data, timestamp, record_start, trigger_source, markers=None, data_b=None, marker_b=None) -> None:
        r, m = self.g["records"], self.g["markers"]
        data = np.asarray(data, np.int16)
        k = len(timestamp)
        if k:
            _ext(r["data"], data.reshape(k, -1))
            _ext(r["timestamp"], np.asarray(timestamp, np.int64))
            _ext(r["record_start"], np.asarray(record_start, np.int64))
            _ext(r["record_number"], np.arange(self.n, self.n + k, dtype=np.uint32))
            _ext(r["trigger_source"], np.asarray(trigger_source, np.uint8))
            if self.b_mode == "records":
                if data_b is None or len(data_b) != k:
                    raise ValueError("Kanal B: Records fehlen (channel_b_trigger = Kanal-A-Trigger)")
                _ext(r["data_b"], np.asarray(data_b, np.int16))
            self.n += k
        if markers is not None and len(markers):
            _ext(m["timestamp"], np.asarray(markers, np.int64))
            if self.b_mode == "markers":
                if marker_b is None or len(marker_b) != len(markers):
                    raise ValueError("Kanal B: Daten zu den Markern fehlen")
                _ext(m["data_b"], np.asarray(marker_b, np.int16))

    def end_block(self) -> int:
        self.counts[self.bid] = self.n
        self.f.flush()
        self.g = None
        return self.n

    def close(self, calibration_attrs: dict[str, Any] | None = None) -> None:
        if self.f is None:
            return
        if calibration_attrs is not None:
            cal = self.f.require_group("calibration")
            S._attrs(cal, calibration_attrs)
        self.f.close()
        self.f = None
