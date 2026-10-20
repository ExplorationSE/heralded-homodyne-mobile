"""Teledyne SP Devices ADQ32 über **ADQAPI** (ctypes), Data-Readout-Schnittstelle.

Abgebildete Funktionen (ADQ3 Series User Guide 21-2539, Rev. 2026.2, Kap. 15 und Anhang A.4; C-Aufrufe mit
``adq_cu`` und ``adq_num`` (ab 1) als ersten Argumenten, Beispiel Kap. 15.6):

=================================  ===========================================================================
Version                            ``ADQAPI_ValidateVersion(major, minor)`` (0 kompatibel, −1 inkompatibel,
                                   −2 rückwärtskompatibel), ``ADQAPI_GetRevisionString`` (optional)
Identifikation                     ``CreateADQControlUnit``, ``ADQControlUnit_EnableErrorTrace``,
                                   ``ADQControlUnit_ListDevices``, ``ADQControlUnit_SetupDevice`` (1 = Erfolg)
Parameter (JSON, Kap. 15.8.2)      ``ADQ_GetParametersString(id, buf, len, format)``,
                                   ``ADQ_SetParametersString(buf, len)`` (Rückgabe < 0 = Fehler)
Erfassung                          ``ADQ_StartDataAcquisition``, ``ADQ_StopDataAcquisition`` (``ADQ_EOK`` oder
                                   ``ADQ_EINTERRUPTED`` erwartet)
Auslese                            ``ADQ_WaitForRecordBuffer(&channel, &buffer, timeout_ms, &status)``
                                   (> 0 Bytes, 0 = nur Status, ``ADQ_EAGAIN`` = Zeitüberschreitung),
                                   ``ADQ_ReturnRecordBuffer(channel, buffer)``
Aufräumen                          ``DeleteADQControlUnit``
=================================  ===========================================================================

Strukturen (Anhang A.3.4, Header-Version 2.0): ``ADQGen4Record {header*, data*, size}`` und
``ADQGen4RecordHeader`` (72 Byte, Layout siehe :class:`ADQGen4RecordHeader`). Werte aus dem Handbuch:
``ADQ_EOK`` 0, ``EINVAL`` −1, ``EAGAIN`` −2, ``EOVERFLOW`` −3, ``ENOTREADY`` −4, ``EINTERRUPTED`` −5, ``EIO`` −6,
``EEXTERNAL`` −7, ``EUNSUPPORTED`` −8, ``EINTERNAL`` −9; ``ADQ_ANY_CHANNEL`` −1; ``ADQ_INFINITE_NOF_RECORDS`` −1;
``ADQAPI_VERSION_MAJOR`` 15, ``MINOR`` 0; Parameter-IDs (``enum ADQParameterId``) wie unten.

Der Header meldet ``time_unit`` (s) und ``sampling_period`` je Record – damit ist die bisher angenommene
``time_unit`` = 1/(8·f_ADC) am Gerät **nachprüfbar** (wird bei jedem Lauf mit dem Geräteattribut verglichen).

**[unsicher, am Gerät zu prüfen]**: genaue JSON-Schlüssel und Aufzählungsnamen in der Praxis (aus den
C-Strukturen abgeleitet; der Treiber liest stets erst die Geräte-JSON und ändert nur bekannte Schlüssel), welche
Eingangsbereiche das ADQ32 annimmt, ob ``adq_num`` nach ``SetupDevice(0)`` immer 1 ist, Verhalten von Kanal B mit
SYNC-Trigger (Marker-Erfassung, siehe :data:`MARKER_CAPTURE`).
"""
from __future__ import annotations

import ctypes as C
import json
import os

import numpy as np

from ._loader import LibraryNotFound, load_library

ADQAPI_VERSION_MAJOR, ADQAPI_VERSION_MINOR = 15, 0
ADQ_EOK, ADQ_EINVAL, ADQ_EAGAIN, ADQ_EOVERFLOW, ADQ_ENOTREADY = 0, -1, -2, -3, -4
ADQ_EINTERRUPTED, ADQ_EIO, ADQ_EEXTERNAL, ADQ_EUNSUPPORTED, ADQ_EINTERNAL = -5, -6, -7, -8, -9
ERRNAMES = {0: "ADQ_EOK", -1: "ADQ_EINVAL", -2: "ADQ_EAGAIN", -3: "ADQ_EOVERFLOW", -4: "ADQ_ENOTREADY",
            -5: "ADQ_EINTERRUPTED", -6: "ADQ_EIO", -7: "ADQ_EEXTERNAL", -8: "ADQ_EUNSUPPORTED", -9: "ADQ_EINTERNAL"}
ADQ_ANY_CHANNEL = -1
ADQ_INFINITE_NOF_RECORDS = -1
LOG_LEVEL_INFO = 2
ADQ_DATA_FORMAT_INT16 = 0
RECORD_STATUS_LOST_DATA, RECORD_STATUS_OVERRANGE = 1 << 0, 1 << 2
READOUT_STARVING, READOUT_INCOMPLETE, READOUT_DISCARDED = 1 << 0, 1 << 1, 1 << 2

PID = dict(DATA_ACQUISITION=1, DATA_TRANSFER=2, DATA_READOUT=3, CONSTANT=4, SAMPLE_SKIP=8, EVENT_SOURCE_PERIODIC=10,
           EVENT_SOURCE_TRIG=11, EVENT_SOURCE_SYNC=12, ANALOG_FRONTEND=13, TOP=19, PORT_TRIG=20, PORT_SYNC=21,
           PORT_GPIOA=27, PULSE_GENERATOR0=31, CLOCK_SYSTEM=40)
REFCLK = {"intern": "ADQ_REFERENCE_CLOCK_SOURCE_INTERNAL", "extern_10MHz": "ADQ_REFERENCE_CLOCK_SOURCE_PORT_CLK"}
EVENT_SOURCE = {"TRIG": "ADQ_EVENT_SOURCE_TRIG", "SYNC": "ADQ_EVENT_SOURCE_SYNC", "PERIODIC": "ADQ_EVENT_SOURCE_PERIODIC",
                "SOFTWARE": "ADQ_EVENT_SOURCE_SOFTWARE"}
EDGE = {"rising": "ADQ_EDGE_RISING", "falling": "ADQ_EDGE_FALLING"}
#: Erfassung der SYNC-Marker am ADQ32 (ED-7). ``channel_b_sync``: Kanal B (LO-Monitor) wird vom SYNC-Port getriggert;
#: die Record-Header von Kanal B liefern die Markerzeiten, ihre Daten den LO-Monitor zu den Markerzeiten (Standard,
#: [unsicher]). ``channel_b_trig``: Kanal B mit dem Trigger von Kanal A (Schema-1.2-Normalfall ``records/data_b``);
#: Markerzeiten dann nicht verfügbar (nur für Prüfungen ohne Synchronisation).
MARKER_CAPTURE = ("channel_b_sync", "channel_b_trig")

LIB_NAMES = {"linux": ["libadq.so", "/usr/lib/libadq.so", "/usr/local/lib/libadq.so"],
             "win": ["ADQAPI.dll", r"C:\Program Files\SP Devices\ADQAPI_x64\ADQAPI.dll"]}
ENV = "HHD_ADQAPI"
HINT = ("Teledyne-SP-Devices-SDK installieren (Linux: TSPD_SDK_linux_<version>.tar.gz laut README → libadq.so und "
        "Kernel-Treiber; Windows: Installer → ADQAPI.dll), siehe docs/HARDWARE.md")


class ADQGen4RecordHeader(C.Structure):
    """Header-Version 2.0 (User Guide A.3.4); natürliche Ausrichtung, 72 Byte."""
    _fields_ = [("version_major", C.c_uint8), ("version_minor", C.c_uint8),
                ("timestamp_synchronization_counter", C.c_uint16), ("general_purpose_start", C.c_uint16),
                ("general_purpose_stop", C.c_uint16), ("timestamp", C.c_uint64), ("record_start", C.c_int64),
                ("record_length", C.c_uint32), ("user_id", C.c_uint8), ("misc", C.c_uint8),
                ("record_status", C.c_uint16), ("record_number", C.c_uint32), ("channel", C.c_uint8),
                ("data_format", C.c_uint8), ("serial_number", C.c_char * 10), ("sampling_period", C.c_uint64),
                ("time_unit", C.c_double), ("firmware_specific", C.c_uint32), ("reserved", C.c_int32)]


class ADQGen4Record(C.Structure):
    _fields_ = [("header", C.POINTER(ADQGen4RecordHeader)), ("data", C.c_void_p), ("size", C.c_uint64)]


class ADQDataReadoutStatus(C.Structure):
    _fields_ = [("flags", C.c_uint32)]


assert C.sizeof(ADQGen4RecordHeader) == 72, "ADQGen4RecordHeader: Layout weicht von Header-Version 2.0 ab"

_vp, _i = C.c_void_p, C.c_int
PROTOTYPES = {
    "ADQAPI_ValidateVersion": ([_i, _i], _i),
    "CreateADQControlUnit": ([], _vp),
    "DeleteADQControlUnit": ([_vp], None),
    "ADQControlUnit_EnableErrorTrace": ([_vp, C.c_uint, C.c_char_p], _i),
    "ADQControlUnit_ListDevices": ([_vp, C.POINTER(_vp), C.POINTER(C.c_uint)], _i),
    "ADQControlUnit_SetupDevice": ([_vp, _i], _i),
    "ADQ_GetParametersString": ([_vp, _i, _i, C.c_char_p, C.c_size_t, _i], _i),
    "ADQ_SetParametersString": ([_vp, _i, C.c_char_p, C.c_size_t], _i),
    "ADQ_StartDataAcquisition": ([_vp, _i], _i),
    "ADQ_StopDataAcquisition": ([_vp, _i], _i),
    "ADQ_WaitForRecordBuffer": ([_vp, _i, C.POINTER(_i), C.POINTER(_vp), _i, C.POINTER(ADQDataReadoutStatus)],
                                C.c_int64),
    "ADQ_ReturnRecordBuffer": ([_vp, _i, _i, _vp], _i),
}
OPTIONAL = {"ADQAPI_ValidateVersion"}


class ADQError(RuntimeError):
    def __init__(self, func: str, code: int, extra: str = ""):
        super().__init__(f"{func}: {ERRNAMES.get(code, code)}" + (f" – {extra}" if extra else "")
                         + " (Ursache im ADQAPI-Trace-Log)")
        self.func, self.code = func, code


class ADQAPILib:
    def __init__(self, path: str | os.PathLike | None = None):
        self.dll = load_library(path, ENV, LIB_NAMES, "ADQ32 (ADQAPI)", HINT)
        self.path = getattr(self.dll, "_name", str(path))
        missing = []
        for name, (args, res) in PROTOTYPES.items():
            try:
                f = getattr(self.dll, name)
            except AttributeError:
                if name not in OPTIONAL:
                    missing.append(name)
                continue
            f.argtypes, f.restype = args, res
        if missing:
            raise LibraryNotFound(f"ADQAPI {self.path}: Funktionen fehlen {missing} – falsche Bibliothek/Version?")

    def revision(self) -> str:
        """Revisionstext der ADQAPI (``ADQAPI_GetRevisionString``, optional; leer, wenn nicht vorhanden)."""
        try:
            f = self.dll.ADQAPI_GetRevisionString
        except AttributeError:
            return ""
        f.argtypes, f.restype = [], C.c_char_p
        try:
            v = f()
        except Exception:  # noqa: BLE001
            return ""
        return v.decode("ascii", "replace") if v else ""

    def validate_version(self) -> int:
        if not hasattr(self.dll, "ADQAPI_ValidateVersion"):
            return 0
        return int(self.dll.ADQAPI_ValidateVersion(ADQAPI_VERSION_MAJOR, ADQAPI_VERSION_MINOR))


class ADQDevice:
    """Ein ADQ3-Digitizer (ADQ32) an einer Control Unit; Parameter über JSON, Auslese über Record-Puffer."""

    def __init__(self, lib: ADQAPILib, device_index: int = 0, trace_dir: str | None = None):
        self.lib, self.device_index, self.trace_dir = lib, int(device_index), trace_dir
        self.cu = None
        self.adq_num = 0
        self.running = False
        self.version_check = None
        self._ch = C.c_int(ADQ_ANY_CHANNEL)
        self._buf = C.c_void_p()
        self._st = ADQDataReadoutStatus()

    def open(self) -> dict:
        v = self.lib.validate_version()
        self.version_check = {0: "kompatibel", -2: "rückwärtskompatibel"}.get(v, "inkompatibel")
        if v == -1:
            raise RuntimeError(f"ADQAPI-Version inkompatibel zu {ADQAPI_VERSION_MAJOR}.{ADQAPI_VERSION_MINOR} "
                               f"(ADQAPI_ValidateVersion = −1; Treiber für diese API-Version prüfen)")
        self.cu = self.lib.dll.CreateADQControlUnit()
        if not self.cu:
            raise RuntimeError("CreateADQControlUnit fehlgeschlagen (Treiber/Kernelmodul geladen?)")
        if self.trace_dir:
            os.makedirs(self.trace_dir, exist_ok=True)
            self.lib.dll.ADQControlUnit_EnableErrorTrace(self.cu, LOG_LEVEL_INFO, str(self.trace_dir).encode())
        lst, n = C.c_void_p(), C.c_uint(0)
        if not self.lib.dll.ADQControlUnit_ListDevices(self.cu, C.byref(lst), C.byref(n)):
            self.close()
            raise RuntimeError("ADQControlUnit_ListDevices fehlgeschlagen")
        if n.value <= self.device_index:
            self.close()
            raise RuntimeError(f"kein ADQ-Digitizer mit Index {self.device_index} gefunden ({n.value} vorhanden)")
        if not self.lib.dll.ADQControlUnit_SetupDevice(self.cu, self.device_index):
            self.close()
            raise RuntimeError(f"ADQControlUnit_SetupDevice({self.device_index}) fehlgeschlagen")
        self.adq_num = 1                     # „Indexing starts at 1“ (Kap. 15.3) – ein Gerät je Control Unit
        return dict(n_devices=int(n.value), adq_num=self.adq_num, version_check=self.version_check,
                    library_path=self.lib.path)

    def close(self) -> None:
        if self.running:
            try:
                self.stop()
            except Exception:  # noqa: BLE001
                pass
        if self.cu:
            self.lib.dll.DeleteADQControlUnit(self.cu)
        self.cu = None

    # ------------------------------------------------------------------ Parameter (JSON)
    def get_params(self, pid: int | str) -> dict:
        pid = PID[pid] if isinstance(pid, str) else int(pid)
        size = 1 << 16
        for _ in range(4):
            buf = C.create_string_buffer(size)
            r = self.lib.dll.ADQ_GetParametersString(self.cu, self.adq_num, pid, buf, size, 0)
            if r >= 0:
                return json.loads(buf.value.decode("ascii", "replace"))
            if r != ADQ_EINVAL:
                raise ADQError("ADQ_GetParametersString", r, f"id {pid}")
            size *= 4                         # Puffer zu klein → größer versuchen (EINVAL laut Handbuch)
        raise ADQError("ADQ_GetParametersString", ADQ_EINVAL, f"id {pid}")

    def set_params(self, params: dict) -> None:
        if self.running:
            raise RuntimeError("ADQ32: Parameter nicht während der Erfassung ändern")
        s = json.dumps(params).encode("ascii")
        r = self.lib.dll.ADQ_SetParametersString(self.cu, self.adq_num, s, len(s) + 1)
        if r < 0:
            raise ADQError("ADQ_SetParametersString", r, str(params.get("id", "")))

    def update(self, pid: str, fn) -> dict:
        """Lesen → ``fn(dict)`` ändert → Schreiben (nur bekannte Schlüssel, Rest bleibt wie vom Gerät)."""
        p = self.get_params(pid)
        fn(p)
        self.set_params(p)
        return p

    # ------------------------------------------------------------------ Erfassung
    def start(self) -> None:
        r = self.lib.dll.ADQ_StartDataAcquisition(self.cu, self.adq_num)
        if r != ADQ_EOK:
            raise ADQError("ADQ_StartDataAcquisition", r)
        self.running = True

    def stop(self) -> None:
        r = self.lib.dll.ADQ_StopDataAcquisition(self.cu, self.adq_num)
        self.running = False
        if r not in (ADQ_EOK, ADQ_EINTERRUPTED):
            raise ADQError("ADQ_StopDataAcquisition", r)

    def wait_record(self, timeout_ms: int):
        """Ein Record von einem beliebigen Kanal. Rückgabe ``None`` (Zeitüberschreitung/nur Status) oder
        ``(channel, header_dict, data int16)``; der Puffer wird sofort zurückgegeben (Daten kopiert)."""
        self._ch.value = ADQ_ANY_CHANNEL
        r = self.lib.dll.ADQ_WaitForRecordBuffer(self.cu, self.adq_num, C.byref(self._ch), C.byref(self._buf),
                                                 int(timeout_ms), C.byref(self._st))
        if r == ADQ_EAGAIN or r == 0:
            return None, int(self._st.flags)
        if r < 0:
            raise ADQError("ADQ_WaitForRecordBuffer", int(r))
        ch = int(self._ch.value)
        rec = C.cast(self._buf, C.POINTER(ADQGen4Record)).contents
        try:
            if not rec.header:
                raise RuntimeError("Record ohne Header (metadata_enabled = 0 oder unvollständiger Record)")
            h = rec.header.contents
            if h.version_major != 2:
                raise RuntimeError(f"Record-Header-Version {h.version_major}.{h.version_minor} ≠ 2.x")
            if h.data_format != ADQ_DATA_FORMAT_INT16:
                raise RuntimeError(f"Datenformat {h.data_format} ≠ ADQ_DATA_FORMAT_INT16")
            n = int(r) // 2
            data = np.ctypeslib.as_array(C.cast(rec.data, C.POINTER(C.c_int16)), shape=(n,)).copy() if n else \
                np.empty(0, np.int16)
            hd = dict(timestamp=int(h.timestamp), record_start=int(h.record_start), record_length=int(h.record_length),
                      record_number=int(h.record_number), record_status=int(h.record_status), channel=int(h.channel),
                      sampling_period=int(h.sampling_period), time_unit=float(h.time_unit),
                      serial=h.serial_number.decode("ascii", "replace"),
                      sync_counter=int(h.timestamp_synchronization_counter), misc=int(h.misc))
        finally:
            rr = self.lib.dll.ADQ_ReturnRecordBuffer(self.cu, self.adq_num, ch, self._buf)
            if rr != ADQ_EOK:
                raise ADQError("ADQ_ReturnRecordBuffer", rr)
        return (ch, hd, data), int(self._st.flags)


# ---------------------------------------------------------------------- Konfiguration für den Homodyn-Aufbau
def put(d: dict, key: str, value) -> None:
    """Bekannten Schlüssel setzen; 64-bit-Ganzzahlen stehen in der JSON als Zeichenkette (Kap. 15.8.2) – der
    Typ des Gerätewerts bleibt erhalten. Unbekannter Schlüssel → ``KeyError`` (früh und deutlich statt still)."""
    if key not in d:
        raise KeyError(f"ADQ-JSON ({d.get('id', '?')}): Schlüssel '{key}' nicht vorhanden – Firmware/ADQAPI-Version "
                       f"prüfen, ggf. Override-Datei anpassen")
    cur = d[key]
    if isinstance(cur, str) and not isinstance(value, str):
        value = str(int(value))
    elif isinstance(cur, float) and isinstance(value, int):
        value = float(value)
    d[key] = value


def merge_override(d: dict, ov: dict) -> None:
    """Override (z. B. aus ``adq_json_override``) rekursiv einmischen; Listen elementweise."""
    for k, v in ov.items():
        if k == "id":
            continue
        if isinstance(v, dict) and isinstance(d.get(k), dict):
            merge_override(d[k], v)
        elif isinstance(v, list) and isinstance(d.get(k), list):
            for i, item in enumerate(v):
                if i < len(d[k]) and isinstance(item, dict) and isinstance(d[k][i], dict):
                    merge_override(d[k][i], item)
                elif i < len(d[k]) and item is not None:
                    d[k][i] = item
        else:
            put(d, k, v)


def configure_homodyne(dev: ADQDevice, *, refclk: str, trigger: str, periodic_hz: float,
                       rec_a: int, pre_a: int, range_a_mV: float, offset_a_mV: float,
                       rec_b: int, pre_b: int, skip_b: int, range_b_mV: float, offset_b_mV: float,
                       trig_threshold_V: float, sync_threshold_V: float, marker_capture: str,
                       overrides: list[dict] | None = None) -> dict:
    """Kanal A (Homodyn) mit TRIG- oder PERIODIC-Ereignis, Kanal B (LO-Monitor) je nach ``marker_capture``.

    Rückgabe: zusammengefasste Einstellungen für die Geräteattribute der HDF5-Datei."""
    if marker_capture not in MARKER_CAPTURE:
        raise ValueError(f"adq_marker_capture: {marker_capture!r} nicht in {MARKER_CAPTURE}")
    src_a = EVENT_SOURCE[trigger]
    src_b = EVENT_SOURCE["SYNC"] if marker_capture == "channel_b_sync" else src_a

    def clk(p):
        put(p, "reference_source", REFCLK[refclk])
    dev.update("CLOCK_SYSTEM", clk)

    def afe(p):
        put(p["channel"][0], "input_range", float(range_a_mV)); put(p["channel"][0], "dc_offset", float(offset_a_mV))
        put(p["channel"][1], "input_range", float(range_b_mV)); put(p["channel"][1], "dc_offset", float(offset_b_mV))
    dev.update("ANALOG_FRONTEND", afe)

    def skip(p):
        put(p["channel"][0], "skip_factor", 1)
        put(p["channel"][1], "skip_factor", int(skip_b))
    dev.update("SAMPLE_SKIP", skip)

    dev.update("EVENT_SOURCE_TRIG", lambda p: put(p["pin"][0], "threshold", float(trig_threshold_V)))
    dev.update("EVENT_SOURCE_SYNC", lambda p: put(p["pin"][0], "threshold", float(sync_threshold_V)))
    if trigger == "PERIODIC":
        dev.update("EVENT_SOURCE_PERIODIC", lambda p: put(p, "frequency", float(periodic_hz)))

    def acq(p):
        for ch, (L, pre, src) in enumerate([(rec_a, pre_a, src_a), (rec_b, pre_b, src_b)]):
            c = p["channel"][ch]
            put(c, "record_length", int(L))
            put(c, "horizontal_offset", -int(pre))
            put(c, "nof_records", ADQ_INFINITE_NOF_RECORDS)
            put(c, "trigger_source", src)
            put(c, "trigger_edge", EDGE["rising"])
    dev.update("DATA_ACQUISITION", acq)

    for ov in overrides or []:
        pid = ov.get("id", "")
        key = pid.replace("ADQ_PARAMETER_ID_", "") if isinstance(pid, str) else pid
        if key not in PID and not isinstance(key, int):
            raise ValueError(f"adq_json_override: unbekannte Parameter-ID {pid!r}")
        dev.update(key, lambda p, ov=ov: merge_override(p, ov))

    const = dev.get_params("CONSTANT")
    return dict(serial=str(const.get("serial_number", "")), product=str(const.get("product_name", "")),
                options=str(const.get("product_options", "")), refclk=refclk, trigger=trigger,
                channel_b_trigger="SYNC" if marker_capture == "channel_b_sync" else trigger,
                marker_capture=marker_capture)
