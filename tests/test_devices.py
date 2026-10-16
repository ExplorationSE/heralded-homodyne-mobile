import numpy as np
import pytest

from hhd.sim.config import SimConfig
from hhd.sim.harp import HydraHarp500, apply_dead_time


def test_dead_time_non_extending():
    t = np.array([0.0, 300e-12, 700e-12, 1000e-12, 1500e-12])
    assert apply_dead_time(t, 680e-12).tolist() == [True, False, True, False, True]


def test_harp_resolution_and_marker_error():
    cfg = SimConfig()
    rng = np.random.default_rng(0)
    t_in = np.sort(rng.uniform(0, 0.1, 5000))
    t_mk = np.arange(1000) * 1e-4 + 5e-5
    hr = HydraHarp500(cfg).record(rng, 0.1, {1: t_in}, {1: t_mk})
    assert hr.times_ps.dtype == np.int64
    ps = hr.per_input_ps[1]
    ok = ps >= 0
    assert np.abs(ps[ok] * 1e-12 - t_in[ok]).max() < 30e-12           # 1 ps Raster + 2,5 ps rms Jitter
    err = hr.per_marker_ps[1] * 1e-12 - t_mk
    assert np.abs(err).max() <= 100e-9 + 1e-12                         # Spezifikation T2: < 100 ns
    assert err.std() == pytest.approx(30e-9, rel=0.15)


def test_digitizer_headers(std_run):
    res, _ = std_run
    tu, fs = res.device_attrs["time_unit_s"], res.device_attrs["sampling_rate_Hz"]
    L, pre = res.device_attrs["record_length"], -res.device_attrs["horizontal_offset"]
    tps = res.device_attrs["sampling_period_tu"]
    assert tps == 8 and tu == pytest.approx(50e-12)
    for br in res.blocks:
        dg = br.digitizer
        if len(dg.timestamp) < 2:
            continue
        # erstes Sample liegt auf dem Sample-Gitter, Pretrigger bis zu einem Sample genau
        assert np.all((dg.timestamp + dg.record_start) % tps == 0)
        assert np.all((-dg.record_start >= pre * tps) & (-dg.record_start < (pre + 1) * tps))
        # Rearm: Records überlappen nicht (Ende + 20 ns ≤ nächster Trigger)
        end = dg.timestamp[:-1] + (L - pre) * tps
        assert np.all(dg.timestamp[1:] * tu >= end * tu + 20e-9 - 1e-12)
        assert dg.data.dtype == np.int16 and dg.data.min() >= -2048 and dg.data.max() <= 2047
    # TRIG-Zeitstempel = Digitizer-Uhr(wahre Triggerzeit), quantisiert auf 50 ps, Jitter 20 ps
    br = [b for b in res.blocks if b.cfg.id == "heralded"][0]
    dg = br.digitizer
    m = np.isfinite(dg.rec_trigger_true)
    dt = dg.timestamp[m] * tu - res.clock(dg.rec_trigger_true[m])
    assert np.all((dt > -50e-12 - 6 * 20e-12) & (dt < 6 * 20e-12))


def test_dead_time_effect_high_rate():
    cfg = SimConfig.from_dict({"harp": {"trigger": "CFD"}})
    rng = np.random.default_rng(4)
    rate = 5e7
    t = np.sort(rng.uniform(0, 1e-3, int(rate * 1e-3)))
    hr = HydraHarp500(cfg).record(rng, 1e-3, {1: t}, {})
    kept = (hr.per_input_ps[1] >= 0).mean()
    assert kept == pytest.approx(1 / (1 + rate * 6.8e-9), rel=0.03)        # nicht verlängernde Totzeit
