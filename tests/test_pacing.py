"""Tempo (Echtzeitfaktor) während eines Laufs ändern (ab 0.15.0): Anker verschieben statt springen/anhalten."""
from __future__ import annotations

from pathlib import Path

import pytest

from hhd.sim.pacing import Paced

ROOT = Path(__file__).resolve().parents[1]


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _paced(clock, speed=1.0):
    p = Paced()
    p._pace_clock = clock
    p.speed = speed
    p._pace_start()
    return p


def test_speed_change_reanchors():
    c = _Clock()
    p = _paced(c, 1.0)
    c.t += 4.0
    p._t_last = 4.0                               # 4 s Laufzeit in 4 s Wanduhr erreicht
    assert p._pace_due(4.0) == pytest.approx(c.t)
    p.speed = 4.0                                 # schneller: ab jetzt 4 s Laufzeit je 1 s, ohne Sprung
    assert p._pace_due(4.0) == pytest.approx(c.t)
    assert p._pace_due(8.0) == pytest.approx(c.t + 1.0)
    c.t += 1.0
    p._t_last = 8.0
    p.speed = 0.5                                 # langsamer: kein Stillstand, nächste Sekunde Laufzeit in 2 s
    assert p._pace_due(9.0) == pytest.approx(c.t + 2.0)
    with pytest.raises(ValueError):
        p.speed = 0


def test_simstream_speed_mid_run(std_run):
    """SimStream: Tempo nach dem ersten Chunk erhöhen → restliche Wartezeit entsprechend kürzer."""
    from hhd.sim.stream import SimStream

    res, _ = std_run
    total = sum(b.duration for b in res.blocks)
    c = _Clock()
    slept = []

    def sleep(w):
        slept.append(w)
        c.t += w

    ss = SimStream(res, chunk_s=0.1, speed=1.0, clock=c, sleep=sleep)
    it = iter(ss)
    next(it)
    assert c.t - 100.0 == pytest.approx(0.1)
    ss.speed = 10.0
    for _ in it:
        pass
    assert c.t - 100.0 == pytest.approx(0.1 + (total - 0.1) / 10.0)


def test_set_display_speed_reaches_backend(tmp_path):
    from hhd.server.service import MeasurementService, ServerConfig
    c = ServerConfig(data_dir=str(tmp_path / "data"), log_dir=str(tmp_path / "logs"), realtime=True, speed=1.0,
                     analysis_inprocess=True, sim_config=str(ROOT / "config/standard.yaml"), auto_analyze=False,
                     harp_rate_selftest=False)
    svc = MeasurementService(c, tmp_path)
    try:
        assert svc.handle("c", "127.0.0.1", True, "acquire_control", {})["ok"]
        m = svc.handle("c", "127.0.0.1", True, "set_display", {"speed": 3.0})
        assert m["ok"], m
        assert svc.display["speed"] == 3.0 and svc.backend.speed == 3.0
        assert not svc.handle("c", "127.0.0.1", True, "set_display", {"speed": 500})["ok"]
    finally:
        svc.close()
