import numpy as np

from hhd.io import ptu as P
from hhd.sim.stream import SimStream, StreamSource


def test_chunks_reassemble_to_file_data(std_run):
    res, d = std_run
    chunks = list(SimStream(res, chunk_s=0.07, realtime=False))
    for br in res.blocks:
        cs = [c for c in chunks if c.block_id == br.cfg.id]
        assert cs[-1].last_in_block and cs[0].meta["attrs"]["dataset_type"] == br.dataset_type
        assert np.array_equal(np.concatenate([c.harp_records for c in cs]), br.harp_records)
        assert np.array_equal(np.concatenate([c.rec_timestamp for c in cs]), br.digitizer.timestamp)
        assert np.array_equal(np.concatenate([c.rec_data for c in cs]), br.digitizer.data)
        assert np.array_equal(np.concatenate([c.marker_timestamp for c in cs]), br.digitizer.marker_ts)
        dec = P.T2Decoder()
        ev = np.concatenate([dec.decode(c.harp_records).event_time_ps for c in cs])
        full = P.read_ptu(d / br.attrs["ptu_file"]).decode()
        assert np.array_equal(ev, full.event_time_ps)


def test_realtime_pacing_with_fake_clock(std_run):
    res, _ = std_run
    now = [0.0]
    slept = []

    def sleep(dt):
        slept.append(dt); now[0] += dt

    n = sum(1 for _ in SimStream(res, chunk_s=0.1, speed=2.0, clock=lambda: now[0], sleep=sleep))
    total = sum(b.duration for b in res.blocks)
    assert now[0] == np.float64(total / 2.0) or abs(now[0] - total / 2.0) < 1e-9
    assert n == sum(int(np.ceil(b.duration / 0.1 - 1e-9)) for b in res.blocks)


def test_stream_source_lifecycle(std_run):
    res, _ = std_run
    src = StreamSource(res, chunk_s=0.5, realtime=False)
    info = src.open()
    assert info["kind"] == "sim" and "heralded" in info["blocks"]
    assert src.read_chunk() is None
    src.start()
    c = src.read_chunk()
    assert c is not None and c.block_id == "kv_start"
    while src.read_chunk() is not None:
        pass
    assert not src.running and src.status()["chunks"] > 3
