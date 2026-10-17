from pathlib import Path

import pytest
import yaml

from hhd.sim.config import BlockCfg, ConfigError, SimConfig, parameter_markdown

ROOT = Path(__file__).resolve().parent.parent


def test_roundtrip_yaml_json(tmp_path):
    c = SimConfig()
    c.save(tmp_path / "c.yaml"); c.save(tmp_path / "c.json")
    assert SimConfig.load(tmp_path / "c.yaml") == c
    assert SimConfig.load(tmp_path / "c.json") == c


def test_partial_config_uses_defaults():
    c = SimConfig.from_dict({"source": {"eta": 0.5}})
    assert c.source.eta == 0.5 and c.source.R_herald == 1000.0 and len(c.blocks) == 18


def test_unknown_keys_rejected():
    with pytest.raises(ConfigError):
        SimConfig.from_dict({"source": {"etaa": 0.5}})
    with pytest.raises(ConfigError):
        SimConfig.from_dict({"quelle": {}})


@pytest.mark.parametrize("patch, msg", [
    ({"harp": {"n_channels": 20}}, "Kanäle"),
    ({"harp": {"model": "HydraHarp 500 S", "n_channels": 4, "marker_timing_channel": 5}}, "außerhalb"),
    ({"harp": {"channel_offsets": {"1": 2e-7}}}, "±100 ns"),
    ({"harp": {"resolution": 2e-12}}, "1 ps"),
    ({"digitizer": {"pretrigger": 2001}}, "Schritten von 8"),
    ({"digitizer": {"channel_mode": 1, "pretrigger": 2008}}, "Schritten von 16"),
    ({"digitizer": {"clock_option": "S5G0"}}, "S2G5"),
    ({"source": {"R_herald": 2e7}}, "10 MHz"),
])
def test_device_limits(patch, msg):
    with pytest.raises(ConfigError, match=msg):
        SimConfig.from_dict(patch).validate()


def test_derived_adq32_timing():
    c = SimConfig()
    assert c.fs == 2.5e9 and c.trig_res == pytest.approx(50e-12) and c.sync_res == pytest.approx(3.2e-9)
    c = SimConfig.from_dict({"digitizer": {"clock_option": "S2G0"}})
    assert c.fs == 2.0e9 and c.trig_res == pytest.approx(62.5e-12) and c.sync_res == pytest.approx(4e-9)
    c = SimConfig.from_dict({"digitizer": {"channel_mode": 1, "pretrigger": 2000}})
    assert c.fs == 5.0e9 and c.trig_res == pytest.approx(50e-12)
    c = SimConfig.from_dict({"digitizer": {"skip_factor": 2}})
    assert c.fs == 1.25e9 and c.trig_res == pytest.approx(50e-12)


def test_usb_warning_and_harp_dead_time():
    c = SimConfig.from_dict({"digitizer": {"form_factor": "USB"}})
    assert any("1,9 GByte/s" in w for w in c.validate())
    assert SimConfig().harp_dead == 680e-12
    assert SimConfig.from_dict({"harp": {"trigger": "CFD"}}).harp_dead == 6.8e-9


def test_presets_valid():
    for p in (ROOT / "config").glob("*.yaml"):
        SimConfig.load(p).validate()


def test_standard_yaml_is_default():
    assert SimConfig.load(ROOT / "config" / "standard.yaml") == SimConfig()


def test_parameter_doc_up_to_date():
    assert (ROOT / "docs" / "PARAMETER.md").read_text(encoding="utf-8") == parameter_markdown(), \
        "docs/PARAMETER.md neu erzeugen: hhd-sim params --markdown > docs/PARAMETER.md"
