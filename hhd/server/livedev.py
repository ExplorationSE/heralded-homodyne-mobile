"""Live-Geräte des Mess-Servers: HydraHarp 500 (``hhd.drivers.hh500``) und ADQ32 (``hhd.drivers.adq``).

Je Gerät ein Erfassungs-Thread (FIFO- bzw. Record-Auslese) mit Warteschlange; der Messablauf ruft ``read`` aus
seinem Thread auf und erhält Datenblöcke wie von den simulierten Geräten (``HarpData``/``DigData``), zusätzlich die
Rohdaten (T2-Records bzw. Header-Zeitstempel in ``time_unit``) für den Datenstrom-Schreiber. ``read`` kehrt nach
≤ 0,25 s zurück (leerer Block, solange nichts vorliegt).

Fehlt die Herstellerbibliothek, meldet ``open`` einen :class:`DeviceError` mit Installationshinweis; der Server
lehnt den Live-Betrieb dann ab, ohne abzustürzen. Geräteeinstellungen werden nie während eines Laufs geschrieben
(beide Handbücher verbieten das); kritische Parameter prüft der Server vorher (ED-8b).
"""
from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .devices import DIG_PARAMS, HARP_PARAMS, DeviceError, DigData, Device, HarpData, RunSpec

_DONE = object()


@dataclass
class LiveCfg:
    """Live-Einstellungen aus der Server-Konfiguration (``[devices]``)."""
    hh500_lib: str = ""                  # leer = Standardorte bzw. HHD_HH500LIB
    hh500_devidx: int = 0
    hh500_serial: str = ""               # leer = Index verwenden
    hh500_debug_dir: str = "logs"
    adq_lib: str = ""                    # leer = Standardorte bzw. HHD_ADQAPI
    adq_device_index: int = 0
    adq_trace_dir: str = "logs/adq_trace"
    adq_marker_capture: str = "channel_b_sync"
    adq_json_override: str = ""          # JSON-Datei: Liste von Parametersätzen {"id": …, …} (nach der Grundeinstellung)
    adq_drain_ms: int = 200              # max. Nachlauf der Auslese nach Laufende (Host-Zeit)
    lo_monitor_skip: int = 25
    split_backlog_mb: float = 512.0      # Rückstau vor dem Zerlege-Prozess, darüber Lauf anhalten (Rate zu hoch)
    split_finish_timeout_s: float = 60.0
    sim_device_attrs: dict = field(default_factory=dict)   # Nennwerte (aus der Simulator-Konfiguration)


class _LiveDevice(Device):
    mode = "live"

    def __init__(self, lc: LiveCfg):
        super().__init__()
        self.lc = lc
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._halt = threading.Event()
        self._done = False
        self.error = ""
        self.hw: dict = {}

    def info(self) -> dict:
        return super().info() | dict(hardware=self.hw, error=self.error)

    def start(self) -> None:
        super().start()
        self._q = queue.Queue()
        self._halt.clear()
        self._done = False
        self._t_start = time.monotonic()
        self._thread = threading.Thread(target=self._acq_wrap, args=(self.armed,), daemon=True,
                                        name=f"acq-{self.kind}")
        self._thread.start()

    def _acq_wrap(self, run: RunSpec) -> None:
        try:
            self._acquire(run)
        except Exception as e:  # noqa: BLE001 – an den Messablauf weiterreichen
            self._q.put(e)
        finally:
            self._q.put(_DONE)

    def _acquire(self, run: RunSpec) -> None:
        raise NotImplementedError

    def _drain(self, wait_s: float = 0.25) -> tuple[list, bool]:
        items, done = [], False
        try:
            it = self._q.get(timeout=wait_s)
            while True:
                if it is _DONE:
                    done = True
                    break
                if isinstance(it, Exception):
                    self.error = f"{type(it).__name__}: {it}"
                    raise DeviceError(f"{self.model}: {self.error}")
                items.append(it)
                it = self._q.get_nowait()
        except queue.Empty:
            pass
        return items, done

    def stop(self) -> None:
        self._halt.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=10)
        self._thread = None
        self.running = False


# ---------------------------------------------------------------------- HydraHarp 500
class HydraHarp500Live(_LiveDevice):
    """HydraHarp 500 S im T2-Modus. Kanalrollen je Lauf aus ``harp_channel_roles`` (Kanal 0 = Sync-Eingang = Marker,
    CH1 Herald, CH2 Kopie der ADQ32-Trigger, …); nur belegte Eingänge werden eingeschaltet."""
    kind, model = "harp", "PicoQuant HydraHarp 500"
    PARAMS = HARP_PARAMS

    def __init__(self, lc: LiveCfg):
        super().__init__(lc)
        self.dev = None
        self._roles = ({0: "sync_marker", 1: "herald"}, {})
        self._split = None                      # Zerlege-Prozess (hhd.io.t2split), ab dem ersten Lauf
        self._finishing = False
        self._t_acq = 0.0

    def open(self) -> dict:
        from ..drivers import LibraryNotFound
        from ..drivers import hh500 as H
        try:
            lib = H.HH500Lib(self.lc.hh500_lib or None)
        except LibraryNotFound as e:
            self.error = str(e)
            raise DeviceError(f"HydraHarp 500 live nicht möglich: {e}") from None
        self.dev = H.HH500Device(lib, self.lc.hh500_devidx, self.lc.hh500_serial or None,
                                 debug_dir=self.lc.hh500_debug_dir or None)
        try:
            self.hw = self.dev.open(H.REFSOURCES[self.params["ref_clock"]])
            self._apply_all()
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            try:
                self.dev.close()
            except Exception:  # noqa: BLE001
                pass
            raise DeviceError(f"HydraHarp 500: {e}") from None
        self.model = f"PicoQuant {self.hw.get('model', 'HydraHarp 500')} SN {self.hw.get('serial', '?')}"
        self.is_open = True
        return self.info()

    def close(self) -> None:
        if self._split is not None:
            self._split.close()
            self._split = None
        if self.dev is not None:
            self.dev.close()
        self.is_open = False

    def count_rate_mcps(self) -> float | None:
        """Summe der Zählraten (Sync + Eingänge) vor Messbeginn (HH500_GetAllCountRates), Mcps."""
        if self.dev is None or not self.is_open:
            return None
        try:
            r = self.dev.rates()
        except Exception:  # noqa: BLE001
            return None
        return (r["sync"] + sum(r["inputs"])) / 1e6

    def _incfg(self, level: float, offset: int = 0, enable: bool = True):
        from ..drivers.hh500 import InputCfg
        p = self.params
        return InputCfg(enable=enable, trg_mode=p["trg_mode"], level_mV=int(round(level)), edge=p["edge"],
                        zerocross_mV=int(p["cfd_zerocross_mV"]), offset_ps=int(offset))

    def _apply_all(self, roles: dict[int, str] | None = None) -> None:
        p, d = self.params, self.dev
        if int(p["sync_divider"]) != 1:
            raise DeviceError("sync_divider: im T2-Modus mit Sync-Eingang als Markerkanal nur 1 (HH500-Handbuch, T2)")
        d.set_sync(self._incfg(p["sync_level_mV"], p["sync_offset_ps"]), 1)
        roles = roles if roles is not None else self._roles[0]
        for ch in range(1, d.n_channels + 1):
            r = roles.get(ch, "")
            on = bool(r)
            if ch == 1 or r == "herald":
                d.set_input(ch - 1, self._incfg(p["herald_level_mV"], p["herald_offset_ps"], on))
            else:
                d.set_input(ch - 1, self._incfg(p["aux_level_mV"], 0, on))
        d.set_markers((bool(p["marker_enable"]),) * 4)
        d.set_trigger_output(10000 if p["trigger_output"] else 0)      # 1 ms Periode (100-ns-Einheiten)

    def _apply(self, new: dict) -> None:
        if not self.is_open:
            return
        from ..drivers import hh500 as H
        if "ref_clock" in new:                                         # Referenz nur über Initialize
            self.dev.close()
            self.hw = self.dev.open(H.REFSOURCES[new["ref_clock"]])
        try:
            self._apply_all()
        except Exception as e:  # noqa: BLE001
            raise DeviceError(f"HydraHarp 500: {e}") from None

    def arm(self, run: RunSpec) -> None:
        super().arm(run)
        from ..io.session import _roles
        a = run.extra.get("attrs", {})
        cr = _roles(a.get("harp_channel_roles")) or {0: "sync_marker", 1: "herald"}
        mr = _roles(a.get("harp_marker_roles"))
        self._roles = (cr, mr)
        self._apply_all({k: v for k, v in cr.items() if k > 0 and not v.startswith("aux")})
        from ..io.t2split import SplitProcessError, T2SplitProcess
        try:
            if self._split is None:
                self._split = T2SplitProcess(max_backlog_mb=float(self.lc.split_backlog_mb))
            self._split.begin_run(self._roles)
        except SplitProcessError as e:
            self._split = None
            raise DeviceError(f"HydraHarp 500: {e}") from None
        self._t_end = 0.0
        self._t_acq = 0.0
        self._finishing = False

    def _acquire(self, run: RunSpec) -> None:
        from ..drivers import hh500 as H
        d = self.dev
        d.start(int(round(run.duration_s * 1000)))
        try:
            while True:
                rec = d.read_fifo()
                if len(rec):
                    self._q.put((rec, time.monotonic() - self._t_start))
                f = d.flags()
                if f & H.FLAG_FIFOFULL:
                    raise RuntimeError("FIFO-Überlauf (FLAG_FIFOFULL) – Daten verloren, Zählraten/USB prüfen")
                if f & H.FLAG_SYSERROR:
                    p = d.debug_dump(time.strftime("%Y%m%d-%H%M%S"), self.lc.hh500_debug_dir)
                    raise RuntimeError(f"Systemfehler (FLAG_SYSERROR), Debug-Dump {p}")
                if self._halt.is_set():
                    break
                if d.ctc_done():                                       # Rest aus dem FIFO holen
                    while True:
                        rec = d.read_fifo()
                        if not len(rec):
                            break
                        self._q.put((rec, time.monotonic() - self._t_start))
                    break
        finally:
            d.stop()

    def read(self):
        """Rohdaten sofort an den Schreiber; Zerlegen im eigenen Prozess (``hhd.io.t2split``), Ergebnisse folgen
        verzögert. Am Laufende zuerst die letzten Records, dann (nächster Aufruf) der Rest der Zerlegung."""
        from ..io.t2split import SplitProcessError
        if not self.running and self._done:
            return None
        try:
            if self._finishing:
                res = self._split.finish_run(timeout_s=float(self.lc.split_finish_timeout_s))
                self._done, self._finishing = True, False
                return self._harp_data(res, np.empty(0, np.uint32), True)
            items, done = self._drain()
            rec = np.concatenate([r for r, _ in items]) if items else np.empty(0, np.uint32)
            if items:
                self._t_acq = max(self._t_acq, items[-1][1])
            if len(rec):
                self._split.submit(rec)              # SplitOverload → Ratenwächter (Service)
            res = self._split.poll()
        except SplitProcessError as e:
            self.error = str(e)
            raise DeviceError(f"HydraHarp 500: {e}") from None
        if done:
            self._finishing = True
        return self._harp_data(res, rec, False)

    def _harp_data(self, res, rec, done: bool) -> HarpData:
        her = np.concatenate([r.herald_s for r in res]) if res else np.empty(0)
        mk = np.concatenate([r.markers_s for r in res]) if res else np.empty(0)
        if res:
            self._t_end = max(self._t_end, max(r.last_ps for r in res) * 1e-12)
        return HarpData(self._t_end, her, mk, len(rec), done, records=rec, t_acq_s=self._t_acq)


# ---------------------------------------------------------------------- ADQ32
class ADQ32Live(_LiveDevice):
    """ADQ32 (FWDAQ): Kanal A Homodyn (TRIG bzw. PERIODIC je Laufart), Kanal B LO-Monitor (PDA05CF2 + 10 dB);
    SYNC-Marker (ED-7) über Kanal B mit SYNC-Trigger (``adq_marker_capture`` = ``channel_b_sync``, [unsicher])."""
    kind, model = "digitizer", "Teledyne SP Devices ADQ32"
    PARAMS = DIG_PARAMS

    def __init__(self, lc: LiveCfg):
        super().__init__(lc)
        self.dev = None
        self.device_attrs: dict = {}
        self.overrides: list[dict] = []

    def open(self) -> dict:
        from ..drivers import LibraryNotFound
        from ..drivers import adq as A
        if self.lc.adq_marker_capture not in A.MARKER_CAPTURE:
            raise DeviceError(f"adq_marker_capture: {self.lc.adq_marker_capture!r} nicht in {A.MARKER_CAPTURE}")
        if self.lc.adq_json_override:
            try:
                ov = json.loads(Path(self.lc.adq_json_override).read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise DeviceError(f"adq_json_override: {e}") from None
            self.overrides = ov if isinstance(ov, list) else [ov]
        try:
            lib = A.ADQAPILib(self.lc.adq_lib or None)
        except LibraryNotFound as e:
            self.error = str(e)
            raise DeviceError(f"ADQ32 live nicht möglich: {e}") from None
        self.dev = A.ADQDevice(lib, self.lc.adq_device_index, self.lc.adq_trace_dir or None)
        try:
            self.hw = self.dev.open()
            self._configure("TRIG", 1000.0)                            # Grundeinstellung prüft alle Schlüssel
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            self.dev.close()
            raise DeviceError(f"ADQ32: {e}") from None
        self.is_open = True
        return self.info()

    def close(self) -> None:
        if self.dev is not None:
            self.dev.close()
        self.is_open = False

    def _configure(self, trigger: str, periodic_hz: float) -> dict:
        from ..drivers import adq as A
        p = self.params
        L, pre, sk = int(p["record_length"]), int(p["pretrigger"]), int(self.lc.lo_monitor_skip)
        s = A.configure_homodyne(
            self.dev, refclk=p["ref_clock"], trigger=trigger, periodic_hz=periodic_hz, rec_a=L, pre_a=pre,
            range_a_mV=p["input_range_mV"], offset_a_mV=p["dc_offset_mV"], rec_b=L // sk, pre_b=pre // sk,
            skip_b=sk, range_b_mV=p["input_range_b_mV"], offset_b_mV=p["dc_offset_b_mV"],
            trig_threshold_V=p["trigger_level_mV"] / 1000.0, sync_threshold_V=p["sync_level_mV"] / 1000.0,
            marker_capture=self.lc.adq_marker_capture, overrides=self.overrides)
        self.hw |= dict(serial=s["serial"], product=s["product"], options=s["options"])
        self.model = f"Teledyne SP Devices {s['product'] or 'ADQ32'} SN {s['serial'] or '?'}"
        nom = dict(self.lc.sim_device_attrs)
        tu = float(nom.get("time_unit_s", 50e-12))
        fs = float(nom.get("f_adc_Hz", 2.5e9))
        self.device_attrs = nom | dict(
            model=f"{s['product'] or 'ADQ32'}-PCIe", serial_number=s["serial"], product_options=s["options"],
            skip_factor=1, sampling_rate_Hz=fs, time_unit_s=tu, record_length=L, horizontal_offset=-pre,
            reference_clock="external 10 MHz" if p["ref_clock"] == "extern_10MHz" else "internal",
            header_reports_trigger_source=False, channel_a_role="homodyne", channel_b_role="lo_monitor",
            channel_b_skip_factor=sk, channel_b_record_length=L // sk, channel_b_horizontal_offset=-(pre // sk),
            channel_b_trigger=s["channel_b_trigger"], adq_marker_capture=s["marker_capture"],
            input_range_mV=p["input_range_mV"], input_range_b_mV=p["input_range_b_mV"], dc_offset_mV=p["dc_offset_mV"],
            dc_offset_b_mV=p["dc_offset_b_mV"], trigger_level_V=p["trigger_level_mV"] / 1000.0,
            sync_level_V=p["sync_level_mV"] / 1000.0, adqapi_library=self.hw.get("library_path", ""),
            device_mode="live")
        self._trigger = trigger
        return s

    def _apply(self, new: dict) -> None:
        if self.is_open:
            try:
                self._configure(getattr(self, "_trigger", "TRIG"), 1000.0)
            except Exception as e:  # noqa: BLE001
                raise DeviceError(f"ADQ32: {e}") from None

    def arm(self, run: RunSpec) -> None:
        super().arm(run)
        a = run.extra.get("attrs", {})
        trig = str(a.get("adq32_trigger_source") or ("TRIG" if run.kind in ("heralded", "herald_blocked", "random", "state")
                                                     else "PERIODIC"))
        if trig not in ("TRIG", "PERIODIC"):
            raise DeviceError(f"Lauf {run.block_id}: Triggerquelle {trig} live nicht vorgesehen")
        try:
            self._configure(trig, float(a.get("periodic_rate") or 2000.0))
        except Exception as e:  # noqa: BLE001
            raise DeviceError(f"ADQ32: {e}") from None
        self._tu_seen = None
        self._pend_a: list = []
        self._pend_b: list = []
        self.n_status = dict(lost=0, overrange=0, readout_flags=0)

    def _acquire(self, run: RunSpec) -> None:
        """Laufdauer in Gerätezeit: Fenster [erster Record, erster Record + T] (Zeitstempel des Headers); Records
        danach werden verworfen. Ende, sobald beide Kanäle das Fenster überschritten haben bzw. nach dem Nachlauf."""
        d = self.dev
        T = float(run.duration_s)
        d.start()
        t0 = time.monotonic()
        ref, past = None, set()
        active = {0, 1}
        try:
            batch, tb = [], time.monotonic()
            while True:
                now = time.monotonic()
                if self._halt.is_set() or now - t0 >= T + self.lc.adq_drain_ms / 1000.0 + 1.0:
                    break
                if now - t0 >= T and past >= active:
                    break
                rec, flags = d.wait_record(50)
                if flags:
                    self.n_status["readout_flags"] |= flags
                if rec is not None:
                    ch, h, _ = rec
                    if ref is None:
                        ref = h["timestamp"]
                    if (h["timestamp"] - ref) * h["time_unit"] > T:
                        past.add(ch)
                    else:
                        batch.append(rec)
                if batch and (time.monotonic() - tb >= 0.1 or len(batch) >= 4096):
                    self._q.put(batch); batch, tb = [], time.monotonic()
            if batch:
                self._q.put(batch)
        finally:
            d.stop()

    def read(self):
        if not self.running and self._done:
            return None
        from ..drivers.adq import RECORD_STATUS_LOST_DATA, RECORD_STATUS_OVERRANGE
        items, done = self._drain()
        self._done = done
        dv = self.device_attrs
        L, Lb = int(dv["record_length"]), int(dv["channel_b_record_length"])
        sync_b = dv.get("channel_b_trigger") == "SYNC"
        mk_ts, mk_b = [], []
        for batch in items:
            for ch, h, data in batch:
                if self._tu_seen is None:
                    self._tu_seen = h["time_unit"]
                    if abs(h["time_unit"] - float(dv["time_unit_s"])) > 1e-15:
                        self.device_attrs["time_unit_s"] = h["time_unit"]      # Header ist maßgeblich
                    self.device_attrs["time_unit_from_header"] = True
                self.n_status["lost"] += bool(h["record_status"] & RECORD_STATUS_LOST_DATA)
                self.n_status["overrange"] += bool(h["record_status"] & RECORD_STATUS_OVERRANGE)
                if ch == 0:
                    if len(data) != L:
                        raise DeviceError(f"ADQ32 Kanal A: Recordlänge {len(data)} ≠ {L}")
                    self._pend_a.append((h["timestamp"], h["record_start"], data))
                elif sync_b:
                    mk_ts.append(h["timestamp"]); mk_b.append(data[:Lb])
                else:
                    self._pend_b.append(data[:Lb])
        if sync_b:
            na = len(self._pend_a)
        else:                                                         # A/B paarweise (gleicher Trigger)
            na = min(len(self._pend_a), len(self._pend_b)) if not done else len(self._pend_a)
        A_, self._pend_a = self._pend_a[:na], self._pend_a[na:]
        B_ = None
        if not sync_b:
            nb = min(na, len(self._pend_b))
            B_, self._pend_b = self._pend_b[:nb], self._pend_b[nb:]
            if len(B_) < na:                                          # fehlende B-Records am Laufende: Dunkelwert
                B_ += [np.zeros(Lb, np.int16)] * (na - len(B_))
        tu = float(self.device_attrs["time_unit_s"])
        ts = np.array([a[0] for a in A_], np.int64)
        rs = np.array([a[1] for a in A_], np.int64)
        data = np.stack([a[2] for a in A_]) if A_ else np.empty((0, L), np.int16)
        db = np.stack(B_) if B_ else (np.empty((0, Lb), np.int16) if not sync_b else None)
        mts = np.array(mk_ts, np.int64)
        mb = np.stack(mk_b) if mk_b else np.empty((0, Lb), np.int16)
        self._n_rec = getattr(self, "_n_rec", 0)
        rn = np.arange(self._n_rec, self._n_rec + len(ts), dtype=np.uint32)
        self._n_rec += len(ts)
        lo_b = db if db is not None else (mb if len(mb) else None)
        raw = dict(timestamp=ts, record_start=rs, trigger_source=np.full(len(ts), 255, np.uint8), markers=mts,
                   data_b=db, marker_b=mb if sync_b else None)
        return DigData(data, lo_b, ts.astype(float) * tu, (ts + rs).astype(float) * tu, np.sort(mts).astype(float) * tu,
                       rn, done, raw=raw)

    def start(self) -> None:
        self._n_rec = 0
        super().start()
