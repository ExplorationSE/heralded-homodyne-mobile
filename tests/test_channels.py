"""Kanalbelegung (Gesamtplan v1.5): HydraHarp 500 S mit Sync-Eingang + 4 Kanälen, ADQ32 Kanal A/B, Trigger-Kopie."""
import json

import h5py
import numpy as np
import pytest

from hhd.io import ptu as P
from hhd.sim.check import check_run
from hhd.sim.cli import validate_run
from hhd.sim.config import ConfigError, SimConfig
from hhd.sim.engine import run

SHORT = [{"id": "kv", "kind": "vacuum", "duration_s": 0.3, "periodic_rate": 2000},
         {"id": "kh", "kind": "herald_blocked", "duration_s": 0.3},
         {"id": "a", "kind": "heralded", "duration_s": 0.6},
         {"id": "b", "kind": "free_running", "duration_s": 0.4, "periodic_rate": 2000},
         {"id": "el", "kind": "electronic", "duration_s": 0.2, "periodic_rate": 2000}]


@pytest.fixture(scope="module")
def ch_run(tmp_path_factory):
    d = tmp_path_factory.mktemp("chan")
    cfg = SimConfig.from_dict({"blocks": SHORT})
    cfg.session.seed = 31
    return run(cfg, d), d


def _blk(res, bid):
    return [b for b in res.blocks if b.cfg.id == bid][0]


def test_default_channel_map():
    c = SimConfig()
    h = c.harp
    assert (c.markers.on_timing_channel, h.marker_timing_channel, h.sync_divider) == (True, 0, 1)
    assert (h.herald_channel, h.trigger_copy_channel, h.herald2_channel, h.random_tag_channel, h.state_tag_channel) == (1, 2, 3, 4, 4)
    assert c.channel_roles("free_running") == {"0": "sync_marker", "1": "herald", "2": "adq32_trigger_copy",
                                               "3": "herald2_reserved", "4": "aux (Zufalls-/Zustandstrigger, sonst Reserve)"}
    assert c.channel_roles("state")["4"] == "state_tag"
    assert c.marker_roles() == {"1": "run_marker", "2": "phase_ref"}         # M3, M4 frei
    assert c.lo_b_enabled and c.lo_b_record_length == 200 and c.lo_b_pretrigger == 80
    assert c.pulse_gen_step == pytest.approx(6.4e-9)


@pytest.mark.parametrize("patch, msg", [
    ({"harp": {"trigger_copy_channel": 1}}, "Kanalbelegung doppelt"),
    ({"harp": {"marker_timing_channel": 1}}, "Kanalbelegung doppelt"),
    ({"harp": {"herald2_channel": 5}}, "außerhalb"),
    ({"harp": {"sync_divider": 2}}, "Sync-Teiler"),
    ({"harp": {"run_marker_bit": 2}}, "Marker-Eingänge doppelt"),
    ({"digitizer": {"channel_mode": 1, "pretrigger": 2000}}, "2-Kanal-Modus"),
    ({"lo_monitor": {"skip_factor": 3}}, "skip_factor"),
    ({"lo_monitor": {"source": "usb"}}, "adq32_b oder off"),
])
def test_channel_config_errors(patch, msg):
    with pytest.raises(ConfigError, match=msg):
        SimConfig.from_dict(patch).validate()


def test_eight_channel_s_model_allows_more():
    c = SimConfig.from_dict({"harp": {"n_channels": 8, "herald2_channel": 5}})
    assert c.validate() is not None


def test_sync_input_carries_markers(ch_run):
    """Sync-Marker am Sync-Eingang: Special-Record Kanal 0 mit 1-ps-Zeitmarke, wie das HH500Lib-Demo dekodiert."""
    res, d = ch_run
    for br in res.blocks:
        ev = P.read_ptu(d / br.attrs["ptu_file"]).decode()
        sync = ev.channel(0)
        tm = br.truth["markers"]
        assert len(sync) == np.count_nonzero(tm["harp_ps"] >= 0) > 0.8 * br.duration * res.cfg.markers.f_marker
        assert np.array_equal(np.sort(tm["harp_ps"][tm["harp_ps"] >= 0]), sync)
        exp = (tm["t_true"] - br.t0 + res.cfg.markers.d_MH) / 1e-12
        assert np.abs(sync - np.sort(exp)[: len(sync)]).max() < 100           # ps: kein 100-ns-Markerfehler
        assert len(ev.markers(1)) == 2                                         # Laufmarke M1: Beginn, Ende
        tags = P.read_ptu(d / br.attrs["ptu_file"]).tags
        assert json.loads(tags["UsrHHD_ChannelRoles"])["0"] == "sync_marker"


def test_trigger_copy(ch_run):
    res, d = ch_run
    q = res.cfg.pulse_gen_step
    # Lauf A: jedes Herald-Ereignis am TRIG-Port erscheint als Kopie auf CH2
    a = _blk(res, "a")
    ev = P.read_ptu(d / a.attrs["ptu_file"]).decode()
    h, cp = ev.channel(1), ev.channel(2)
    assert len(cp) == len(h) == len(a.truth["heralds"]["t_true"])
    dt = (cp - h) * 1e-12
    c = res.cfg
    nominal = c.digitizer.d_T + c.digitizer.trigger_copy_latency + c.harp.d_TC - c.harp.d_H
    assert np.all(dt > nominal - 1e-9) and np.all(dt < nominal + q + 1e-9)    # Raster 6,4 ns
    # Lauf B (freilaufend): Kopie der periodischen Trigger → Record-Zeit im Harp-Zeitbereich
    b = _blk(res, "b")
    ev = P.read_ptu(d / b.attrs["ptu_file"]).decode()
    cp = ev.channel(2) * 1e-12 + b.t0
    rec_D = b.digitizer.timestamp * c.time_unit
    rec_true = res.clock.inverse(rec_D)
    assert len(cp) == len(rec_true)
    off = cp - rec_true
    assert np.ptp(off) < q + 1e-9 and np.median(off) == pytest.approx(c.digitizer.trigger_copy_latency + c.harp.d_TC, abs=q)
    assert b.attrs["adq32_trigger_source"] == "PERIODIC" and a.attrs["adq32_trigger_source"] == "TRIG"


def test_channel_b_lo_monitor(ch_run):
    res, d = ch_run
    assert validate_run(d) == []
    lm = res.cfg.lo_monitor
    with h5py.File(d / "digitizer.h5") as f:
        dev = f["device"].attrs
        assert dev["channel_a_role"] == "homodyne" and dev["channel_b_role"] == "lo_monitor"
        assert dev["channel_b_record_length"] == 200 and dev["channel_b_skip_factor"] == 25
        for bid in ("kv", "a", "b"):
            db = f[f"blocks/{bid}/records/data_b"]
            assert db.attrs["channel"] == "B" and db.shape == (f[f"blocks/{bid}/records/data"].shape[0], 200)
        el = f["blocks/el/records/data_b"][:]
        assert el.mean() == pytest.approx(lm.dark_lsb, abs=0.5)                 # LO aus: Dunkelwert
    r = check_run(d, n_boot=5)
    assert r["comparison"]["drift"]["lo_monitor"]["rel_change"] == pytest.approx(0.0, abs=0.003)


def test_external_monitor_optional_and_b_off(tmp_path):
    cfg = SimConfig.from_dict({"blocks": SHORT[:1], "drift": {"monitor": True}, "lo_monitor": {"source": "off"}})
    res = run(cfg, tmp_path)
    assert res.blocks[0].digitizer.data_b is None and res.blocks[0].monitor is not None
    assert validate_run(tmp_path) == []
    with h5py.File(tmp_path / "digitizer.h5") as f:
        assert "data_b" not in f["blocks/kv/records"] and "monitor" in f["blocks/kv"]


def test_validation_detects_missing_channel_b(ch_run, tmp_path):
    import shutil
    _, d = ch_run
    dd = tmp_path / "k"
    shutil.copytree(d, dd)
    with h5py.File(dd / "digitizer.h5", "a") as f:
        del f["blocks/a/records/data_b"]
    from hhd.io.schema import validate_digitizer_file
    assert any("data_b fehlt" in e for e in validate_digitizer_file(dd / "digitizer.h5"))
