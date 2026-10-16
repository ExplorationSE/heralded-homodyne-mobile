import json

import h5py
import numpy as np
import pytest

from hhd.io import ptu as P
from hhd.sim.cli import validate_run
from hhd.sim.config import BlockCfg, SimConfig
from hhd.sim.engine import run, simulate


def _blk(res, bid):
    return [b for b in res.blocks if b.cfg.id == bid][0]


def test_files_and_schema(std_run):
    res, d = std_run
    for name in ("run.json", "digitizer.h5", "truth.h5", "harp_heralded.ptu", "harp_kv_start.ptu"):
        assert (d / name).exists()
    assert validate_run(d) == []
    man = json.loads((d / "run.json").read_text(encoding="utf-8"))
    assert [b["dataset_type"] for b in man["blocks"]] == ["vacuum", "herald_blocked", "heralded", "random_trigger", "vacuum"]
    with h5py.File(d / "digitizer.h5") as f:
        ids = {f[f"blocks/{b}"].attrs["calibration_id"] for b in f["blocks"]}
        assert len(ids) == 1                                    # gemeinsame Kalibrierung aller Vergleichsdatensätze
        assert json.loads(f["calibration"].attrs["vacuum_blocks"]) == ["kv_start", "kv_end"]
        assert f["device"].attrs["model"] == "ADQ32-PCIe (SIMULATED)"
    tags = P.read_ptu(d / "harp_heralded.ptu").tags
    assert tags["UsrHHD_DatasetType"] == "heralded" and "HydraHarp 500" in tags["HW_Type"]


def test_deterministic_and_seed_dependent(tmp_path):
    cfg = SimConfig()
    cfg.blocks = [BlockCfg(id="kv", kind="vacuum", duration_s=0.05, periodic_rate=2000),
                  BlockCfg(id="h", kind="heralded", duration_s=0.2, random_rate=100)]
    a, b = run(cfg, tmp_path / "a"), run(cfg, tmp_path / "b")
    assert (tmp_path / "a/harp_h.ptu").read_bytes() == (tmp_path / "b/harp_h.ptu").read_bytes()
    assert np.array_equal(_blk(a, "h").digitizer.data, _blk(b, "h").digitizer.data)
    cfg.run.seed += 1
    c = simulate(cfg)
    assert not np.array_equal(_blk(a, "h").harp_records, _blk(c, "h").harp_records)


def test_block_physics(std_run):
    res, _ = std_run
    kv, kh, h, rnd = (_blk(res, i) for i in ("kv_start", "kh", "heralded", "random"))
    assert len(kv.truth["modes"]["x"]) == 0 and len(kh.truth["modes"]["x"]) == 0
    assert np.all(kv.digitizer.rec_kind == 2) and len(kv.digitizer.timestamp) == pytest.approx(0.5 * 4000, abs=2)
    assert np.all(kh.digitizer.rec_kind == 0)
    assert set(np.unique(h.digitizer.rec_kind)) == {0, 1}            # Herald + eingestreute Zufallstrigger
    assert np.all(rnd.digitizer.rec_kind == 1)
    # ungeheraldete Signalphotonen: Rate R_h (1/η_h − 1)
    k = h.truth["modes"]["kind"]
    assert (k == 1).sum() / max((k == 0).sum(), 1) == pytest.approx(1 / res.cfg.source.eta_herald - 1, rel=0.1)


def test_herald_record_alignment(std_run):
    """Herald in der PTU-Datei ↔ Record im Digitizer: Δt = d_T − d_H (bis auf Jitter/Auflösung)."""
    res, _ = std_run
    h = _blk(res, "heralded")
    c = res.cfg
    tu = res.device_attrs["time_unit_s"]
    rec = h.truth["heralds"]["record"]
    hps = h.truth["heralds"]["harp_ps"]
    m = (rec >= 0) & (hps >= 0)
    mp = h.expected["mapping"]
    tD_pred = mp["slope"] * hps[m] * 1e-12 + mp["offset_s"]
    dt = h.digitizer.timestamp[rec[m]] * tu - tD_pred
    assert np.abs(dt - (c.digitizer.d_T - c.harp.d_H)).max() < 0.5e-9
    # jeder Zufallstrigger-Record hat seine Kennung auf dem Harp-Kanal
    rr = h.truth["random"]["record"]
    assert np.all(h.truth["random"]["harp_ps"][rr >= 0] >= 0)


def test_phase_reference_markers(phase_run):
    res, d = phase_run
    ramp, steps = _blk(res, "coh_ramp"), _blk(res, "coh_steps")
    ev = P.read_ptu(d / "harp_coh_ramp.ptu").decode()
    assert len(ev.markers(2)) == int(ramp.duration * 10.0)
    ev = P.read_ptu(d / "harp_coh_steps.ptu").decode()
    assert len(ev.markers(2)) == 4
    th = steps.truth["modes"]["theta"]
    assert set(np.round(np.unique(th), 6)) <= set(np.round(res.cfg.phase.steps, 6))
    with h5py.File(d / "digitizer.h5") as f:
        assert f["blocks/coh_ramp"].attrs["phase_mode"] == "ramp"
        assert f["blocks/coh_ramp"].attrs["phase_ramp_Hz"] == 10.0


def test_free_clock_mapping(free_run):
    """Freie Uhr: Harp-Marker (Marker-Eingang) ↔ SYNC-Zeitstempel ergeben Steigung 1 + ε."""
    res, d = free_run
    h = _blk(res, "heralded")
    ev = P.read_ptu(d / "harp_heralded.ptu").decode()
    assert len(ev.markers(res.cfg.harp.marker_bit)) > 1500
    mk = h.truth["markers"]
    both = (mk["harp_ps"] >= 0) & mk["recorded_by_digitizer"]
    tH = mk["harp_ps"][both] * 1e-12
    tD = h.digitizer.marker_ts[np.cumsum(mk["recorded_by_digitizer"])[both] - 1] * res.device_attrs["time_unit_s"]
    slope = np.polyfit(tH, tD, 1)[0]
    assert slope - 1 == pytest.approx(res.cfg.clock.eps, abs=2e-8)
