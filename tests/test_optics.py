"""Gesamtplan v1.6: 1550 nm, Herald-Detektor-Presets (SNSPD / InGaAs-APD), LO-Monitor PDA05CF2 mit Dämpfer."""
import json

import h5py
import numpy as np
import pytest

from hhd.io import ptu as P
from hhd.sim.config import FWHM, HERALD_PRESETS, ConfigError, SimConfig, lo_monitor_levels
from hhd.sim.engine import run

SHORT = [dict(id="kv", kind="vacuum", duration_s=0.2, periodic_rate=4000.0),
         dict(id="a", kind="heralded", duration_s=0.5)]


def test_presets_and_explicit_override():
    s = SimConfig().source
    assert s.det_eff == 0.85 and s.R_dark == 100.0 and s.det_dead_s == 30e-9
    assert s.sigma_spd * FWHM == pytest.approx(40e-12) and s.eta_herald == pytest.approx(0.85 * 0.6)
    a = SimConfig.from_dict({"optics": {"herald_detector": "ingaas_apd"}}).source
    assert (a.det_eff, a.R_dark, a.det_dead_s) == (0.20, 3000.0, 10e-6) and a.eta_herald == pytest.approx(0.12)
    assert a.sigma_spd * FWHM == pytest.approx(150e-12)
    o = SimConfig.from_dict({"optics": {"herald_detector": "ingaas_apd"}, "source": {"R_dark": 800.0}}).source
    assert o.R_dark == 800.0 and o.det_eff == 0.20                 # ausdrücklich angegeben → bleibt
    assert set(HERALD_PRESETS) == {"snspd", "ingaas_apd"}


def test_lo_monitor_chain_levels():
    lev = lo_monitor_levels(1.0, 5000.0, 10.0, 150e-6, 0.005, 2e-3, -0.2)
    assert lev["v_signal"] == pytest.approx(150e-6 * 5000 * 10 ** -0.5) and lev["v_max_at_adc"] < 1.75
    c = SimConfig()
    assert c.lo_monitor.lsb_nominal == pytest.approx(lev["lsb_nominal"])
    assert -2048 < c.lo_monitor.dark_lsb and c.lo_monitor.dark_lsb + 1.2 * c.lo_monitor.lsb_nominal < 2047
    with pytest.raises(ConfigError, match="1,75 V"):
        SimConfig.from_dict({"lo_monitor": {"attenuator_dB": 3.0}}).validate()
    with pytest.raises(ConfigError, match="eta_hd_qe"):
        SimConfig.from_dict({"source": {"eta": 0.97}}).validate()


def test_dead_time_and_metadata(tmp_path):
    cfg = SimConfig.from_dict({"optics": {"herald_detector": "ingaas_apd"}, "source": {"det_dead_s": 200e-6},
                               "blocks": SHORT, "session": {"seed": 3}})
    assert any("Totzeit" in w for w in cfg.validate())
    res = run(cfg, tmp_path)
    man = json.loads((tmp_path / "session.json").read_text(encoding="utf-8"))
    assert man["optics"]["wavelength_nm"] == 1550 and man["optics"]["herald_detector"] == "ingaas_apd"
    with h5py.File(tmp_path / "digitizer.h5") as f:
        assert f["device"].attrs["wavelength_nm"] == 1550 and f["device"].attrs["lo_monitor_pd"] == "PDA05CF2"
    tags = P.read_ptu(tmp_path / "harp_a.ptu").tags
    assert tags["UsrHHD_Wavelength_nm"] == 1550 and tags["UsrHHD_HeraldDetector"] == "ingaas_apd"
    # Totzeit: kein Abstand der (wahren) Herald-Zeiten unter 200 µs; Rate ≤ 1/Totzeit
    t = np.sort(res.blocks[1].truth["heralds"]["t_true"])
    assert len(t) > 100 and np.diff(t).min() >= 200e-6
    assert len(t) / 0.5 < 1 / 200e-6
    # verlorene Heralds bleiben als ungeheraldete Signalphotonen erhalten
    pairs = res.blocks[1].truth["pairs"]
    assert (~pairs["heralded"]).sum() > 0
