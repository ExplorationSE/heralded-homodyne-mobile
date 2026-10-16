"""PicoQuant PTU-Dateien im Record-Typ *Generic T2* (0x00010207).

Format laut öffentlicher PicoQuant-Dokumentation und Demo-Lesern
(``PicoQuant-Time-Tagged-File-Format-Demos``, ``Read_PTU.py``, Funktion ``readHT2(2)``)
sowie HydraHarp-500-Handbuch, Kap. 6.3 (T2: 32 bit je Record, 6 bit Kanal, 25 bit Zeitmarke,
Überlauf-Records) **[Spez.]**:

* Bit 31 ``special``, Bits 30..25 ``channel`` (6 bit), Bits 24..0 ``timetag`` (25 bit).
* ``special = 1, channel = 63``: Überlauf; ``timetag`` = Anzahl der Überläufe (Wrap 2^25).
* ``special = 1, channel = 1..15``: Marker, ``channel`` ist die Bitmaske der Marker-Eingänge.
* ``special = 1, channel = 0``: Ereignis am Sync-Eingang (in T2 ein normaler Kanal, hier „Kanal 0“).
* ``special = 0``: Ereignis am Eingang ``channel + 1``.

Zeitauflösung in T2: 1 ps (``MeasDesc_GlobalResolution = 1e-12``) **[Spez. HydraHarp 500]**.
Dass die HydraHarp 500 den Typ *Generic T2* schreibt, stammt aus der snAPI-Dokumentation
(Vorbericht 2) und ist am Gerät noch nicht geprüft **[unsicher]**.

Dieses Modul enthält keinen Code von PicoQuant; es ist eine eigene Implementierung des Formats.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np

T2_WRAP = 1 << 25                     # 33 554 432 (HydraHarp V2 / Generic)
REC_GENERIC_T2 = 0x00010207

TY_EMPTY8 = 0xFFFF0008
TY_BOOL8 = 0x00000008
TY_INT8 = 0x10000008
TY_FLOAT8 = 0x20000008
TY_ANSI = 0x4001FFFF

KIND_EVENT = 0      # Eingangskanal 1..N
KIND_MARKER = 1     # Marker-Bitmaske 1..15
KIND_SYNC = 2       # Sync-Eingang (Kanal 0)


class PtuError(ValueError):
    pass


# ---------------------------------------------------------------------------- Kodierung
def encode_t2(times_ps: np.ndarray, kinds: np.ndarray, codes: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Kodiert Ereignisse in T2-Records (uint32, Little Endian).

    ``times_ps`` int64 ≥ 0, ``kinds`` (KIND_*), ``codes`` = Kanal 1..63 bzw. Marker-Bitmaske 1..15.
    Die Ereignisse werden stabil nach Zeit sortiert. Rückgabe: (records, record_time_ps); die Zeit eines
    Überlauf-Records ist der Beginn der Wrap-Periode, ab der er gilt (für die zeitliche Aufteilung im
    Datenstrom-Modus).
    """
    t = np.asarray(times_ps, dtype=np.int64)
    k = np.asarray(kinds, dtype=np.int64)
    c = np.asarray(codes, dtype=np.int64)
    if not (len(t) == len(k) == len(c)):
        raise PtuError("times/kinds/codes: ungleiche Längen")
    if len(t) and t.min() < 0:
        raise PtuError("negative Zeitmarken")
    if np.any((k == KIND_EVENT) & ((c < 1) | (c > 63))):
        raise PtuError("Eingangskanal außerhalb 1..63")
    if np.any((k == KIND_MARKER) & ((c < 1) | (c > 15))):
        raise PtuError("Marker-Bitmaske außerhalb 1..15")
    o = np.argsort(t, kind="stable")
    t, k, c = t[o], k[o], c[o]
    wrap = t // T2_WRAP
    tt = t % T2_WRAP
    word = np.where(k == KIND_EVENT, ((c - 1) << 25) | tt,
                    np.where(k == KIND_MARKER, (1 << 31) | (c << 25) | tt, (1 << 31) | tt))
    # Überläufe vor jedem Ereignis, dessen Wrap-Periode wechselt
    prev = np.concatenate([[0], wrap[:-1]]) if len(wrap) else wrap
    jump = wrap - prev
    n_ofl_rec = np.where(jump > 0, (jump + (T2_WRAP - 2)) // (T2_WRAP - 1), 0)
    total = len(t) + int(n_ofl_rec.sum())
    out = np.empty(total, dtype=np.uint32)
    out_t = np.empty(total, dtype=np.int64)
    if int(n_ofl_rec.sum()) == 0:
        out[:] = word.astype(np.uint32)
        out_t[:] = t
        return out, out_t
    pos = 0
    for i in range(len(t)):            # selten: nur Ereignisse mit Periodenwechsel brauchen Überläufe
        j = int(jump[i])
        if j > 0:
            base = int(prev[i])
            while j > 0:
                m = min(j, T2_WRAP - 1)
                base += m
                out[pos] = (1 << 31) | (63 << 25) | m
                out_t[pos] = base * T2_WRAP
                pos += 1
                j -= m
        out[pos] = word[i]
        out_t[pos] = t[i]
        pos += 1
    return out, out_t


@dataclass
class T2Events:
    """Dekodierte T2-Ereignisse (Zeiten in ps seit Messbeginn)."""
    event_time_ps: np.ndarray
    event_channel: np.ndarray       # 1..64 für Eingänge, 0 für Sync
    marker_time_ps: np.ndarray
    marker_bits: np.ndarray

    def channel(self, ch: int) -> np.ndarray:
        return self.event_time_ps[self.event_channel == ch]

    def markers(self, bit_mask: int) -> np.ndarray:
        return self.marker_time_ps[(self.marker_bits & bit_mask) != 0]


@dataclass
class T2Decoder:
    """Zustandsbehafteter T2-Dekoder (Überlaufzähler über Blockgrenzen), wie im Datenstrom-Modus nötig."""
    ofl: int = 0
    n_records: int = 0

    def decode(self, rec: np.ndarray) -> T2Events:
        r = np.asarray(rec, dtype=np.uint32).astype(np.int64)
        special = r >> 31
        chan = (r >> 25) & 0x3F
        ttag = r & 0x1FFFFFF
        is_ofl = (special == 1) & (chan == 63)
        n_ofl = np.where(is_ofl, np.where(ttag == 0, 1, ttag), 0)
        cum = self.ofl + np.cumsum(n_ofl)
        t = cum * T2_WRAP + ttag
        ev = special == 0
        sy = (special == 1) & (chan == 0)
        mk = (special == 1) & (chan >= 1) & (chan <= 15)
        if len(r):
            self.ofl = int(cum[-1])
        self.n_records += len(r)
        evm = ev | sy
        ch = np.where(ev, chan + 1, 0)
        return T2Events(t[evm], ch[evm], t[mk], chan[mk])


# ---------------------------------------------------------------------------- Kopf
def _tag(name: str, typ: int, value: Any = 0, idx: int = -1) -> bytes:
    b = name.encode("ascii")[:32].ljust(32, b"\0") + struct.pack("<iI", idx, typ)
    if typ == TY_ANSI:
        s = str(value).encode("utf-8") + b"\0"
        s = s.ljust((len(s) + 7) // 8 * 8, b"\0")
        return b + struct.pack("<q", len(s)) + s
    if typ == TY_FLOAT8:
        return b + struct.pack("<d", float(value))
    if typ == TY_BOOL8:
        return b + struct.pack("<q", -1 if value else 0)
    return b + struct.pack("<q", int(value))


def build_header(n_records: int, extra_tags: dict[str, tuple[int, Any]] | None = None,
                 comment: str = "", hw_type: str = "HydraHarp 500 (SIMULATED)",
                 acquisition_time_ms: int | None = None) -> bytes:
    hdr = b"PQTTTR\0\0" + b"1.0.00\0\0"
    hdr += _tag("File_Comment", TY_ANSI, comment)
    hdr += _tag("HW_Type", TY_ANSI, hw_type)
    hdr += _tag("Measurement_Mode", TY_INT8, 2)
    hdr += _tag("Measurement_SubMode", TY_INT8, 0)
    hdr += _tag("TTResultFormat_TTTRRecType", TY_INT8, REC_GENERIC_T2)
    hdr += _tag("TTResultFormat_BitsPerRecord", TY_INT8, 32)
    hdr += _tag("MeasDesc_GlobalResolution", TY_FLOAT8, 1e-12)
    hdr += _tag("MeasDesc_Resolution", TY_FLOAT8, 1e-12)
    if acquisition_time_ms is not None:
        hdr += _tag("MeasDesc_AcquisitionTime", TY_INT8, int(acquisition_time_ms))
    hdr += _tag("TTResult_SyncRate", TY_INT8, 0)
    hdr += _tag("TTResult_NumberOfRecords", TY_INT8, n_records)
    for name, (typ, val) in (extra_tags or {}).items():
        hdr += _tag(name, typ, val)
    hdr += _tag("Header_End", TY_EMPTY8, 0)
    return hdr


def write_ptu(path: str | Path, records: np.ndarray, **header_kw) -> int:
    records = np.asarray(records, dtype="<u4")
    with open(path, "wb") as fh:
        fh.write(build_header(len(records), **header_kw))
        fh.write(records.tobytes())
    return len(records)


@dataclass
class PtuFile:
    tags: dict[str, Any] = field(default_factory=dict)
    records: np.ndarray = field(default_factory=lambda: np.empty(0, np.uint32))

    def decode(self) -> T2Events:
        return T2Decoder().decode(self.records)


def read_ptu(path: str | Path) -> PtuFile:
    data = Path(path).read_bytes()
    if data[:6] != b"PQTTTR":
        raise PtuError("keine PTU-Datei (Kennung PQTTTR fehlt)")
    pos = 16
    tags: dict[str, Any] = {}
    while True:
        if pos + 48 > len(data):
            raise PtuError("Kopf ohne Header_End")
        name = data[pos:pos + 32].rstrip(b"\0").decode("ascii", "replace")
        idx, typ = struct.unpack_from("<iI", data, pos + 32)
        pos += 40
        if typ == TY_ANSI:
            n = struct.unpack_from("<q", data, pos)[0]
            pos += 8
            val: Any = data[pos:pos + n].split(b"\0", 1)[0].decode("utf-8", "replace")
            pos += n
        elif typ == TY_FLOAT8:
            val = struct.unpack_from("<d", data, pos)[0]
            pos += 8
        elif (typ & 0xFFFF) == 0xFFFF:       # sonstige Felder variabler Länge (Arrays, Blobs)
            n = struct.unpack_from("<q", data, pos)[0]
            pos += 8 + n
            val = None
        else:
            val = struct.unpack_from("<q", data, pos)[0]
            pos += 8
        tags[name if idx < 0 else f"{name}({idx})"] = val
        if name == "Header_End":
            break
    rt = int(tags.get("TTResultFormat_TTTRRecType", -1)) & 0xFFFFFFFF
    if rt != REC_GENERIC_T2:
        raise PtuError(f"Record-Typ {rt:#010x} nicht unterstützt (erwartet Generic T2 0x00010207)")
    n = int(tags["TTResult_NumberOfRecords"])
    if pos + 4 * n > len(data):
        raise PtuError("Datei kürzer als TTResult_NumberOfRecords")
    rec = np.frombuffer(data, dtype="<u4", offset=pos, count=n).copy()
    return PtuFile(tags, rec)


def iter_chunks(records: np.ndarray, record_time_ps: np.ndarray, edges_ps: Iterable[int]):
    """Teilt Records an Zeitgrenzen (für den Datenstrom-Modus); jede Grenze ist ein Zeitpunkt in ps."""
    idx = np.searchsorted(record_time_ps, np.asarray(list(edges_ps), dtype=np.int64), side="left")
    lo = 0
    for hi in list(idx) + [len(records)]:
        yield records[lo:hi]
        lo = hi
