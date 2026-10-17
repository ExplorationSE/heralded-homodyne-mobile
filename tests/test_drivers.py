"""Treiber HH500Lib/ADQAPI (hhd.drivers) gegen die Mock-Bibliotheken: Signaturen, Fehlercodes, FIFO/Record-Puffer."""
from __future__ import annotations

import ctypes as C
import json

import numpy as np
import pytest

from hhd.drivers import LibraryNotFound
from hhd.drivers import adq as A
from hhd.drivers import hh500 as H
from hhd.io.ptu import T2Decoder


def test_loader_reports_all_candidates(tmp_path, monkeypatch):
    monkeypatch.setenv("HHD_HH500LIB", str(tmp_path / "env" / "libhh500lib.so"))
    with pytest.raises(LibraryNotFound) as e:
        H.HH500Lib(tmp_path / "explizit" / "libhh500lib.so")
    msg = str(e.value)
    assert "explizit" in msg and "env" in msg and "/opt/picoquant/hh500lib/libhh500lib.so" in msg and "HHD_HH500LIB" in msg
    monkeypatch.delenv("HHD_ADQAPI", raising=False)
    with pytest.raises(LibraryNotFound, match="libadq.so"):
        A.ADQAPILib(tmp_path / "fehlt.so")


def test_wrong_library_detected(mock_libs):
    with pytest.raises(LibraryNotFound, match="Funktionen fehlen"):
        H.HH500Lib(mock_libs["adq"])                         # falsche Bibliothek → fehlende Funktionen


def test_hh500_prototypes_and_errors(mock_libs, monkeypatch, tmp_path):
    lib = H.HH500Lib(mock_libs["hh500"])
    for name, args in H.PROTOTYPES.items():
        assert list(getattr(lib.dll, name).argtypes) == args and getattr(lib.dll, name).restype is C.c_int
    assert lib.library_version() == "1.0"
    dev = H.HH500Device(lib, 0)
    hw = dev.open(H.REF_INTERNAL)
    assert hw["n_channels"] == 4 and hw["base_resolution_ps"] == 1.0 and hw["serial"]
    with pytest.raises(H.HH500Error, match="INVALID_ARGUMENT"):
        dev.set_input(0, H.InputCfg(level_mV=-5000))         # Pegel außerhalb ±1200 mV
    with pytest.raises(ValueError, match="Eingang 5"):
        dev.set_input(4, H.InputCfg())
    with pytest.raises(H.HH500Error):
        dev.set_sync(H.InputCfg(), divider=2)                # T2 mit Sync als Timing-Kanal: Teiler 1
    dev.set_sync(H.InputCfg(level_mV=-50))
    for ch in range(2):
        dev.set_input(ch, H.InputCfg(level_mV=-80, offset_ps=0))
    dev.set_input(2, H.InputCfg(enable=False)); dev.set_input(3, H.InputCfg(enable=False))
    dev.set_markers((True,) * 4)
    dev.start(250)
    with pytest.raises(RuntimeError, match="nicht während"):
        dev.set_sync(H.InputCfg())                           # Treiber verhindert Setxxx während der Messung
    with pytest.raises(H.HH500Error, match="BUSY"):
        lib.call("HH500_SetSyncDiv", 0, 1)                   # Bibliothek ebenso
    parts = []
    while True:
        parts.append(dev.read_fifo())
        if dev.ctc_done():
            while True:
                r = dev.read_fifo()
                if not len(r):
                    break
                parts.append(r)
            break
    dev.stop()
    rec = np.concatenate(parts)
    ev = T2Decoder().decode(rec)
    sync = ev.event_time_ps[ev.event_channel == 0] * 1e-12
    her = ev.event_time_ps[ev.event_channel == 1] * 1e-12
    cp = ev.event_time_ps[ev.event_channel == 2] * 1e-12
    assert 180 < len(sync) < 250 and np.all(np.diff(ev.event_time_ps) >= 0)          # 1 kHz, 10 % ausgelassen
    assert np.allclose(np.mod(sync - 15e-9, 1e-3), 0.5e-3, atol=1e-9)                # Marker (k + ½) ms + 15 ns
    assert len(her) > 100 and len(cp) == len(her) and np.allclose(cp - her, 52.7e-9, atol=2e-12)
    assert max(ev.event_time_ps) < 0.26e12 and (rec >> 25 & 0x7F).max() == 0x7F      # Überlaufrecords vorhanden
    assert dev.flags() & H.FLAG_FIFOFULL == 0
    dev.close()
    monkeypatch.setenv("MOCK_HH500_NODEV", "1")
    with pytest.raises(RuntimeError, match="nicht gefunden"):
        H.HH500Device(lib, 0).open()
    monkeypatch.delenv("MOCK_HH500_NODEV")
    monkeypatch.setenv("MOCK_HH500_SYSERROR", "1")
    d2 = H.HH500Device(lib, 0, debug_dir=str(tmp_path / "dbg"))
    with pytest.raises(H.HH500Error, match="INIT"):
        d2.open()
    d2.close()
    assert len(list((tmp_path / "dbg").glob("hh500_debugdump_initialize-*.bin"))) == 1   # Debug-Dump im Log-Ordner


def test_adq_params_records_and_errors(mock_libs, monkeypatch, tmp_path):
    lib = A.ADQAPILib(mock_libs["adq"])
    dev = A.ADQDevice(lib, 0, str(tmp_path / "trace"))
    info = dev.open()
    assert info["adq_num"] == 1 and info["version_check"] == "kompatibel"
    assert (tmp_path / "trace" / "spd_adqcontrolunit_trace.log").exists()
    acq = dev.get_params("DATA_ACQUISITION")
    assert acq["id"] == "ADQ_PARAMETER_ID_DATA_ACQUISITION" and isinstance(acq["channel"][0]["record_length"], str)
    with pytest.raises(A.ADQError, match="ADQ_EINVAL"):
        dev.start()                                          # kein Kanal aktiv
    with pytest.raises(KeyError, match="gibt_es_nicht"):
        dev.update("ANALOG_FRONTEND", lambda p: A.put(p["channel"][0], "gibt_es_nicht", 1))
    with pytest.raises(A.ADQError, match="ADQ_EINVAL"):
        dev.update("ANALOG_FRONTEND", lambda p: A.put(p["channel"][0], "input_range", 333.0))
    s = A.configure_homodyne(dev, refclk="extern_10MHz", trigger="TRIG", periodic_hz=1000, rec_a=1000, pre_a=400,
                             range_a_mV=500, offset_a_mV=0, rec_b=40, pre_b=16, skip_b=25, range_b_mV=1000,
                             offset_b_mV=-100, trig_threshold_V=0.3, sync_threshold_V=0.5,
                             marker_capture="channel_b_sync",
                             overrides=[{"id": "ADQ_PARAMETER_ID_DATA_ACQUISITION", "channel": [{"rearm_length": 64}]}])
    assert s["channel_b_trigger"] == "SYNC" and s["product"] == "ADQ32"
    acq = dev.get_params("DATA_ACQUISITION")
    assert acq["channel"][0]["record_length"] == "1000" and acq["channel"][0]["horizontal_offset"] == "-400"
    assert acq["channel"][1]["trigger_source"] == "ADQ_EVENT_SOURCE_SYNC" and acq["channel"][0]["rearm_length"] == "64"
    assert dev.get_params("CLOCK_SYSTEM")["reference_source"] == "ADQ_REFERENCE_CLOCK_SOURCE_PORT_CLK"
    dev.start()
    with pytest.raises(RuntimeError, match="nicht während"):
        dev.set_params(acq)
    got = {0: [], 1: []}
    import time
    t0 = time.monotonic()
    while time.monotonic() - t0 < 0.3:
        rec, _ = dev.wait_record(50)
        if rec:
            got[rec[0]].append(rec)
    dev.stop()
    a, b = got[0], got[1]
    assert len(a) > 100 and 200 < len(b) < 320
    h = a[0][1]
    assert h["time_unit"] == 50e-12 and h["sampling_period"] == 8 and h["record_length"] == 1000
    assert h["record_start"] < 0 and len(a[0][2]) == 1000 and a[0][2].dtype == np.int16
    hb = b[0][1]
    assert hb["sampling_period"] == 200 and hb["timestamp"] % 64 == 0 and len(b[0][2]) == 40   # SYNC: 3,2-ns-Raster
    tb = np.array([x[1]["timestamp"] for x in b]) * 50e-12
    assert np.allclose(np.mod(tb - 23e-9, 1e-3), 0.5e-3, atol=4e-9)
    with pytest.raises(A.ADQError, match="ENOTREADY"):
        dev.stop()                                           # schon gestoppt
    monkeypatch.setenv("MOCK_ADQ_OVERFLOW_AFTER", "5")
    dev.start()
    with pytest.raises(A.ADQError, match="EOVERFLOW"):
        for _ in range(100):
            dev.wait_record(100)
    dev.stop()
    dev.close()
    monkeypatch.setenv("MOCK_ADQ_VERSION", "-1")
    with pytest.raises(RuntimeError, match="inkompatibel"):
        A.ADQDevice(lib).open()
    monkeypatch.delenv("MOCK_ADQ_VERSION")
    monkeypatch.setenv("MOCK_ADQ_NODEV", "1")
    with pytest.raises(RuntimeError, match="kein ADQ-Digitizer"):
        A.ADQDevice(lib).open()


def test_adq_header_layout():
    assert C.sizeof(A.ADQGen4RecordHeader) == 72
    off = {f[0]: getattr(A.ADQGen4RecordHeader, f[0]).offset for f in A.ADQGen4RecordHeader._fields_}
    assert off["timestamp"] == 8 and off["record_start"] == 16 and off["record_length"] == 24
    assert off["record_number"] == 32 and off["channel"] == 36 and off["serial_number"] == 38
    assert off["sampling_period"] == 48 and off["time_unit"] == 56 and off["reserved"] == 68
    assert json.dumps(A.PID)
