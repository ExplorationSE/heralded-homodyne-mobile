"""PicoQuant HydraHarp 500 über die native Bibliothek ``HH500Lib`` (ctypes), T2-Modus.

Abgebildete Funktionen (HH500Lib Programming Library for Linux v1.0.0.0, Kap. 7.2; Rückgabe < 0 = Fehler):

=============================  ==============================================================================
Allgemein                      ``HH500_GetLibraryVersion``, ``HH500_GetErrorString``
Gerät                          ``HH500_OpenDevice``, ``HH500_CloseDevice``, ``HH500_Initialize(devidx, mode=2 (T2),
                               refsource)``, ``HH500_GetHardwareInfo``, ``HH500_GetSerialNumber``,
                               ``HH500_GetNumOfInputChannels``, ``HH500_GetBaseResolution``, ``HH500_SaveDebugDump``
Sync-Eingang                   ``HH500_SetSyncDiv``, ``HH500_SetSyncTrgMode``, ``HH500_SetSyncEdgeTrg``,
                               ``HH500_SetSyncCFD``, ``HH500_SetSyncChannelOffset``, ``HH500_SetSyncChannelEnable``
Eingänge (Index 0..N−1)        ``HH500_SetInputTrgMode``, ``HH500_SetInputEdgeTrg``, ``HH500_SetInputCFD``,
                               ``HH500_SetInputChannelOffset``, ``HH500_SetInputChannelEnable``
Marker / Trigger-Ausgang       ``HH500_SetMarkerEdges``, ``HH500_SetMarkerEnable``, ``HH500_SetTriggerOutput``
Messung                        ``HH500_SetMeasControl``, ``HH500_StartMeas(tacq ms)``, ``HH500_StopMeas``,
                               ``HH500_CTCStatus``, ``HH500_ReadFiFo(buffer[TTREADMAX], &nactual)``,
                               ``HH500_GetElapsedMeasTime``
Zustand                        ``HH500_GetFlags``, ``HH500_GetWarnings``, ``HH500_GetWarningsText``,
                               ``HH500_GetSyncRate``, ``HH500_GetAllCountRates``
=============================  ==============================================================================

Konstanten, deren Zahlenwert im Handbuch steht: Modus T2 = 2; Referenz 0 = intern, 1 = 10 MHz extern; Trigger-Modus
ETR = 0, CFD = 1; Flanke 0 = fallend, 1 = steigend; ``MEASCTRL_SINGLESHOT_CTC`` = 0, ``MEASCTRL_SW_START_SW_STOP`` = 6;
Flags ``FIFOFULL`` 0x2, ``REF_LOST`` 0x8, ``SYSERROR`` 0x10, ``ACTIVE`` 0x20, ``CNTS_DROPPED`` 0x40; Puffergröße
``TTREADMAX`` = 1 048 576 Records. **[unsicher, am Gerät/an hh500defin.h prüfen]**: Grenzwerte TRGLVLMIN/MAX,
CHANOFFSMIN/MAX, CFD-Grenzen, ACQTMAX (100 h laut Handbuch) und Puffergrößen für Zeichenketten (Handbuch: Version ≥ 8,
Fehlertext ≥ 40, Seriennummer ≥ 8 Zeichen; hier großzügig 64 bzw. 16 384 für den Warnungstext).
"""
from __future__ import annotations

import ctypes as C
import os
from dataclasses import dataclass

import numpy as np

from ._loader import load_library

MODE_T2 = 2
REF_INTERNAL, REF_EXT_10MHZ = 0, 1
REFSOURCES = {"intern": REF_INTERNAL, "extern_10MHz": REF_EXT_10MHZ, "white_rabbit": 3}   # 3 = WR-Slave (generisch)
TRGMODE_ETR, TRGMODE_CFD = 0, 1
EDGE_FALLING, EDGE_RISING = 0, 1
MEASCTRL_SINGLESHOT_CTC, MEASCTRL_SW_START_SW_STOP = 0, 6
FLAG_OVERFLOW, FLAG_FIFOFULL, FLAG_SYNC_LOST, FLAG_REF_LOST = 0x1, 0x2, 0x4, 0x8
FLAG_SYSERROR, FLAG_ACTIVE, FLAG_CNTS_DROPPED = 0x10, 0x20, 0x40
TTREADMAX = 1 << 20
ACQTMAX_MS = 100 * 3600 * 1000            # 100 h (Handbuch: Grenze des Hardware-CTC)
MAXDEVNUM = 8

LIB_NAMES = {"linux": ["/opt/picoquant/hh500lib/libhh500lib.so", "libhh500lib.so"],
             "win": ["HH500Lib.dll", r"C:\Windows\System32\HH500Lib.dll"]}
ENV = "HHD_HH500LIB"
HINT = ("HH500Lib von PicoQuant installieren (Linux: 'sudo ./install' → /opt/picoquant/hh500lib/libhh500lib.so, "
        "udev-Regel HydraHarp500.rules; Windows: setup.exe → HH500Lib.dll), siehe docs/HARDWARE.md")

_i, _u, _d = C.c_int, C.c_uint, C.c_double
_pi, _pu, _pd, _s = C.POINTER(C.c_int), C.POINTER(C.c_uint), C.POINTER(C.c_double), C.c_char_p
PROTOTYPES = {
    "HH500_GetLibraryVersion": [_s],
    "HH500_GetErrorString": [_s, _i],
    "HH500_OpenDevice": [_i, _s],
    "HH500_CloseDevice": [_i],
    "HH500_Initialize": [_i, _i, _i],
    "HH500_GetHardwareInfo": [_i, _s, _s, _s],
    "HH500_GetSerialNumber": [_i, _s],
    "HH500_GetNumOfInputChannels": [_i, _pi],
    "HH500_GetBaseResolution": [_i, _pd, _pi],
    "HH500_SaveDebugDump": [_i, _s],
    "HH500_SetSyncDiv": [_i, _i],
    "HH500_SetSyncTrgMode": [_i, _i],
    "HH500_SetSyncEdgeTrg": [_i, _i, _i],
    "HH500_SetSyncCFD": [_i, _i, _i],
    "HH500_SetSyncChannelOffset": [_i, _i],
    "HH500_SetSyncChannelEnable": [_i, _i],
    "HH500_SetInputTrgMode": [_i, _i, _i],
    "HH500_SetInputEdgeTrg": [_i, _i, _i, _i],
    "HH500_SetInputCFD": [_i, _i, _i, _i],
    "HH500_SetInputChannelOffset": [_i, _i, _i],
    "HH500_SetInputChannelEnable": [_i, _i, _i],
    "HH500_SetMarkerEdges": [_i, _i, _i, _i, _i],
    "HH500_SetMarkerEnable": [_i, _i, _i, _i, _i],
    "HH500_SetTriggerOutput": [_i, _i],
    "HH500_SetMeasControl": [_i, _i, _i, _i],
    "HH500_StartMeas": [_i, _i],
    "HH500_StopMeas": [_i],
    "HH500_CTCStatus": [_i, _pi],
    "HH500_ReadFiFo": [_i, _pu, _pi],
    "HH500_GetElapsedMeasTime": [_i, _pd],
    "HH500_GetFlags": [_i, _pi],
    "HH500_GetWarnings": [_i, _pi],
    "HH500_GetWarningsText": [_i, _s, _i],
    "HH500_GetSyncRate": [_i, _pi],
    "HH500_GetAllCountRates": [_i, _pi, _pi],
}
OPTIONAL = {"HH500_GetWarningsText", "HH500_SaveDebugDump", "HH500_GetAllCountRates"}


class HH500Error(RuntimeError):
    def __init__(self, func: str, code: int, text: str):
        super().__init__(f"{func}: Fehler {code} ({text})")
        self.func, self.code, self.text = func, code, text


class HH500Lib:
    """Dünne ctypes-Bindung: jede Funktion mit Prototyp; Fehler (< 0) → :class:`HH500Error` mit Klartext."""

    def __init__(self, path: str | os.PathLike | None = None):
        self.dll = load_library(path, ENV, LIB_NAMES, "HydraHarp 500 (HH500Lib)", HINT)
        self.path = getattr(self.dll, "_name", str(path))
        missing = []
        for name, args in PROTOTYPES.items():
            try:
                f = getattr(self.dll, name)
            except AttributeError:
                if name not in OPTIONAL:
                    missing.append(name)
                continue
            f.argtypes, f.restype = args, C.c_int
        if missing:
            from ._loader import LibraryNotFound
            raise LibraryNotFound(f"HH500Lib {self.path}: Funktionen fehlen {missing} – falsche Bibliothek/Version?")

    def has(self, name: str) -> bool:
        return hasattr(self.dll, name)

    def error_string(self, code: int) -> str:
        buf = C.create_string_buffer(64)
        try:
            self.dll.HH500_GetErrorString(buf, int(code))
            return buf.value.decode("ascii", "replace") or "?"
        except Exception:  # noqa: BLE001
            return "?"

    def call(self, name: str, *args) -> int:
        r = getattr(self.dll, name)(*args)
        if r < 0:
            raise HH500Error(name, r, self.error_string(r))
        return r

    def library_version(self) -> str:
        buf = C.create_string_buffer(16)
        self.call("HH500_GetLibraryVersion", buf)
        return buf.value.decode("ascii", "replace")


@dataclass
class InputCfg:
    """Einstellung eines Timing-Eingangs (Sync = Kanal ``None``)."""
    enable: bool = True
    trg_mode: str = "ETR"            # ETR | CFD
    level_mV: int = -100             # ETR: Triggerpegel; CFD: Diskriminatorpegel
    edge: str = "falling"            # ETR: falling | rising
    zerocross_mV: int = 10           # CFD: Nulldurchgangspegel
    offset_ps: int = 0


class HH500Device:
    """Ein HydraHarp-500-Gerät im T2-Modus (Ablauf laut Handbuch Kap. 5.2 und Demo ``tttrmode``)."""

    def __init__(self, lib: HH500Lib, devidx: int = 0, serial: str | None = None, debug_dir: str | None = None):
        self.lib, self.devidx, self.want_serial = lib, int(devidx), serial
        self.debug_dir = debug_dir or None
        self.serial = ""
        self.is_open = self.initialized = self.running = False
        self.n_channels = 0
        self.base_resolution_ps = 1.0
        self.hw: dict = {}
        self._buf = np.empty(TTREADMAX, dtype=np.uint32)
        self._bufp = self._buf.ctypes.data_as(C.POINTER(C.c_uint))
        self._n = C.c_int(0)

    # ------------------------------------------------------------------ Öffnen
    def open(self, refsource: int = REF_INTERNAL) -> dict:
        """Gerät öffnen (bei gesuchter Seriennummer alle Indizes 0..7 prüfen) und für T2 initialisieren."""
        idxs = range(MAXDEVNUM) if self.want_serial else [self.devidx]
        last = None
        for i in idxs:
            sn = C.create_string_buffer(64)
            try:
                self.lib.call("HH500_OpenDevice", i, sn)
            except Exception as e:  # noqa: BLE001
                last = e
                continue
            s = sn.value.decode("ascii", "replace")
            if self.want_serial and s != self.want_serial:
                self.lib.dll.HH500_CloseDevice(i)
                continue
            self.devidx, self.serial, self.is_open = i, s, True
            break
        if not self.is_open:
            raise RuntimeError(f"HydraHarp 500 nicht gefunden/geöffnet (Index {list(idxs)}"
                               + (f", Seriennummer {self.want_serial}" if self.want_serial else "") + f"): {last}")
        try:
            self.lib.call("HH500_Initialize", self.devidx, MODE_T2, int(refsource))
        except Exception:
            self.debug_dump("initialize-" + __import__("time").strftime("%Y%m%d-%H%M%S"))
            raise
        self.initialized = True
        model, part, ver = (C.create_string_buffer(64) for _ in range(3))
        self.lib.call("HH500_GetHardwareInfo", self.devidx, model, part, ver)
        n = C.c_int(0)
        self.lib.call("HH500_GetNumOfInputChannels", self.devidx, C.byref(n))
        res, steps = C.c_double(0), C.c_int(0)
        self.lib.call("HH500_GetBaseResolution", self.devidx, C.byref(res), C.byref(steps))
        self.n_channels, self.base_resolution_ps = int(n.value), float(res.value)
        self.hw = dict(model=model.value.decode("ascii", "replace"), partno=part.value.decode("ascii", "replace"),
                       version=ver.value.decode("ascii", "replace"), serial=self.serial, n_channels=self.n_channels,
                       base_resolution_ps=self.base_resolution_ps, library_version=self.lib.library_version(),
                       library_path=self.lib.path, devidx=self.devidx, refsource=int(refsource))
        return self.hw

    def close(self) -> None:
        if self.running:
            self.stop()
        if self.is_open:
            self.lib.dll.HH500_CloseDevice(self.devidx)
        self.is_open = self.initialized = False

    def debug_dump(self, tag: str, directory: str | None = None) -> str | None:
        """``HH500_SaveDebugDump`` nach SYSERROR/Initialisierungsfehler (Handbuch Kap. 6.7)."""
        if not self.lib.has("HH500_SaveDebugDump"):
            return None
        d = directory or self.debug_dir
        if not d:
            import tempfile
            d = tempfile.gettempdir()
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"hh500_debugdump_{tag}.bin")
        try:
            self.lib.dll.HH500_SaveDebugDump(self.devidx, p.encode())
            return p
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ Einstellungen (nie während der Messung)
    def _guard(self):
        if not self.initialized:
            raise RuntimeError("HydraHarp 500 nicht initialisiert")
        if self.running:
            raise RuntimeError("HH500_Setxxx nicht während einer TTTR-Messung (Handbuch, Abschnitt TTTR-Demos)")

    def set_sync(self, ic: InputCfg, divider: int = 1) -> None:
        self._guard()
        d, L = self.devidx, self.lib
        L.call("HH500_SetSyncDiv", d, int(divider))
        L.call("HH500_SetSyncChannelEnable", d, 1 if ic.enable else 0)
        if ic.trg_mode == "CFD":
            L.call("HH500_SetSyncTrgMode", d, TRGMODE_CFD)
            L.call("HH500_SetSyncCFD", d, int(ic.level_mV), int(ic.zerocross_mV))
        else:
            L.call("HH500_SetSyncTrgMode", d, TRGMODE_ETR)
            L.call("HH500_SetSyncEdgeTrg", d, int(ic.level_mV), EDGE_RISING if ic.edge == "rising" else EDGE_FALLING)
        L.call("HH500_SetSyncChannelOffset", d, int(ic.offset_ps))

    def set_input(self, ch_index: int, ic: InputCfg) -> None:
        """``ch_index`` 0..N−1 (Frontplatte 1..N)."""
        self._guard()
        if not 0 <= ch_index < self.n_channels:
            raise ValueError(f"Eingang {ch_index + 1} gibt es nicht (Gerät hat {self.n_channels})")
        d, L = self.devidx, self.lib
        L.call("HH500_SetInputChannelEnable", d, ch_index, 1 if ic.enable else 0)
        if not ic.enable:
            return
        if ic.trg_mode == "CFD":
            L.call("HH500_SetInputTrgMode", d, ch_index, TRGMODE_CFD)
            L.call("HH500_SetInputCFD", d, ch_index, int(ic.level_mV), int(ic.zerocross_mV))
        else:
            L.call("HH500_SetInputTrgMode", d, ch_index, TRGMODE_ETR)
            L.call("HH500_SetInputEdgeTrg", d, ch_index, int(ic.level_mV),
                   EDGE_RISING if ic.edge == "rising" else EDGE_FALLING)
        L.call("HH500_SetInputChannelOffset", d, ch_index, int(ic.offset_ps))

    def set_markers(self, enable: tuple[bool, bool, bool, bool], rising: tuple[bool, bool, bool, bool] = (True,) * 4):
        self._guard()
        self.lib.call("HH500_SetMarkerEdges", self.devidx, *[1 if r else 0 for r in rising])
        self.lib.call("HH500_SetMarkerEnable", self.devidx, *[1 if e else 0 for e in enable])

    def set_trigger_output(self, period_100ns: int) -> None:
        """Programmierbarer Triggerausgang (Einheit 100 ns, 0 = aus). Laserschutz beachten – nur lokal (ED-8b)."""
        self._guard()
        self.lib.call("HH500_SetTriggerOutput", self.devidx, int(period_100ns))

    # ------------------------------------------------------------------ Messung
    def start(self, tacq_ms: int | None) -> None:
        """``tacq_ms`` None → Software-Start/-Stopp (MEASCTRL_SW_START_SW_STOP, ohne 100-h-Grenze)."""
        if not self.initialized:
            raise RuntimeError("HydraHarp 500 nicht initialisiert")
        if tacq_ms is None or tacq_ms > ACQTMAX_MS:
            self.lib.call("HH500_SetMeasControl", self.devidx, MEASCTRL_SW_START_SW_STOP, 0, 0)
            tacq_ms = ACQTMAX_MS
        else:
            self.lib.call("HH500_SetMeasControl", self.devidx, MEASCTRL_SINGLESHOT_CTC, 0, 0)
        self.lib.call("HH500_StartMeas", self.devidx, max(1, int(tacq_ms)))
        self.running = True

    def read_fifo(self) -> np.ndarray:
        """Ein Aufruf ``HH500_ReadFiFo`` (kehrt nach ≈ 10 ms zurück); Rückgabe: Kopie der T2-Records (uint32)."""
        self.lib.call("HH500_ReadFiFo", self.devidx, self._bufp, C.byref(self._n))
        n = int(self._n.value)
        if n < 0 or n > TTREADMAX:
            raise RuntimeError(f"HH500_ReadFiFo: unplausible Anzahl {n}")
        return self._buf[:n].copy()

    def ctc_done(self) -> bool:
        s = C.c_int(0)
        self.lib.call("HH500_CTCStatus", self.devidx, C.byref(s))
        return bool(s.value)

    def stop(self) -> None:
        if self.is_open:
            self.lib.call("HH500_StopMeas", self.devidx)       # auch nach Ablauf aufrufen (Handbuch)
        self.running = False

    def flags(self) -> int:
        f = C.c_int(0)
        self.lib.call("HH500_GetFlags", self.devidx, C.byref(f))
        return int(f.value)

    def elapsed_ms(self) -> float:
        e = C.c_double(0)
        self.lib.call("HH500_GetElapsedMeasTime", self.devidx, C.byref(e))
        return float(e.value)

    def rates(self) -> dict:
        sync = C.c_int(0)
        cr = (C.c_int * max(self.n_channels, 1))()
        if self.lib.has("HH500_GetAllCountRates"):
            self.lib.call("HH500_GetAllCountRates", self.devidx, C.byref(sync), cr)
        else:
            self.lib.call("HH500_GetSyncRate", self.devidx, C.byref(sync))
        return dict(sync=int(sync.value), inputs=[int(x) for x in cr[: self.n_channels]])

    def warnings(self) -> tuple[int, str]:
        """Warnungen (erst nach dem Lesen der Zählraten gültig, Handbuch Kap. 5.4)."""
        self.rates()
        w = C.c_int(0)
        self.lib.call("HH500_GetWarnings", self.devidx, C.byref(w))
        txt = ""
        if w.value and self.lib.has("HH500_GetWarningsText"):
            buf = C.create_string_buffer(16384)
            self.lib.call("HH500_GetWarningsText", self.devidx, buf, int(w.value))
            txt = buf.value.decode("ascii", "replace").strip()
        return int(w.value), txt
