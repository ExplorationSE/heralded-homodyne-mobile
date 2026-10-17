"""Import: PTU T2 (HydraHarp 500), ADQ32-HDF5 1.2 inkl. records/data_b, Manifest/Sitzung/Kampagne."""
import json

import numpy as np
import pytest

from hhd.io import ptu as P
from hhd.io.session import (FORBIDDEN_FILES, RUN_KEYS, SESSION_KEYS, is_campaign, load_campaign, load_session)
from hhd.sim.config import BlockCfg, SimConfig
from hhd.sim.engine import run_campaign


def test_session_summary(std_run):
    res, d = std_run
    s = load_session(d)
    assert not s.warnings
    assert [r.block_id for r in s.runs] == [b.id for b in res.cfg.blocks]
    for r in s.runs:
        sm = r.summary()
        assert sm["n_records"] == r.n_records > 0
        assert sm["harp"]["sync_marker"] > 100 and sm["n_sync_D"] > 100
    h = s.run("heralded")
    assert h.dataset_type == "heralded" and h.harp.role_s("random_tag").size > 0
    assert np.all(np.diff(h.trigger_D()) > 0)
    assert np.all(h.first_sample_D() < h.trigger_D())


def test_ptu_chunked_equals_full(std_run):
    _, d = std_run
    r = load_session(d).run("heralded")
    full = P.read_ptu(r.ptu_path).decode()
    dec = P.T2Decoder()
    parts = [dec.decode(c) for c in P.iter_ptu_records(r.ptu_path, chunk_records=777)]
    t = np.concatenate([p.event_time_ps for p in parts])
    ch = np.concatenate([p.event_channel for p in parts])
    assert np.array_equal(t, full.event_time_ps) and np.array_equal(ch, full.event_channel)
    hs = list(r.iter_harp(chunk_records=1000))
    n = sum(len(x.role_ps("herald")) for x in hs)
    assert n == len(r.harp.role_ps("herald"))
    tags, off = P.read_ptu_header(r.ptu_path)
    assert off > 0
    assert "UsrHHD_ChannelRoles" in tags


def test_read_records_and_channel_b(std_run):
    _, d = std_run
    r = load_session(d).run("heralded")
    idx = np.array([0, 3, 4, 200, 201, r.n_records - 1])
    a = r.read_records(idx)
    assert a.shape == (6, r.record_length) and a.dtype == np.int16
    allr = np.concatenate([b for _, b in r.iter_records(chunk=100)])
    assert np.array_equal(a, allr[idx])
    if r.has_data_b:
        b = r.read_records(idx, channel="B")
        assert b.shape[0] == 6


def test_manifest_whitelist_no_truth(std_run):
    _, d = std_run
    s = load_session(d)
    flat = json.dumps(s.manifest, default=str) + json.dumps([r.attrs for r in s.runs], default=str)
    for bad in ("expected", "truth", "config_json"):
        assert f'"{bad}"' not in flat
    assert set(s.manifest) <= set(SESSION_KEYS) | {"runs", "calibration", "files"}
    assert not set(s.manifest["files"]) & set(FORBIDDEN_FILES)
    for r in s.manifest.get("runs", []):
        assert set(r) <= set(RUN_KEYS)
    with pytest.raises(PermissionError):
        from hhd.io.session import _guard
        _guard(d / FORBIDDEN_FILES[0])


def test_campaign(tmp_path):
    cfg = SimConfig()
    cfg.session.seed = 4
    cfg.blocks = [BlockCfg(id="kv", kind="vacuum", duration_s=0.2, periodic_rate=2000),
                  BlockCfg(id="a", kind="heralded", duration_s=0.4)]
    run_campaign(cfg, tmp_path, n_sessions=2)
    assert is_campaign(tmp_path)
    c = load_campaign(tmp_path)
    assert len(c.sessions) == 2 and not c.warnings
    assert len({s.session_id for s in c.sessions}) == 2
