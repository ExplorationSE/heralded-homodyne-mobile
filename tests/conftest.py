import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hhd.sim.config import BlockCfg, SimConfig  # noqa: E402
from hhd.sim.engine import run  # noqa: E402


def small_standard(seed: int = 11) -> SimConfig:
    """Standard-Messablauf, verkürzt (ca. 3000 heralded Records)."""
    cfg = SimConfig()
    cfg.run.seed = seed
    cfg.blocks = [BlockCfg(id="kv_start", kind="vacuum", duration_s=0.5, periodic_rate=4000),
                  BlockCfg(id="kh", kind="herald_blocked", duration_s=0.5),
                  BlockCfg(id="heralded", kind="heralded", duration_s=3.0, random_rate=200),
                  BlockCfg(id="random", kind="random", duration_s=0.5, random_rate=2000),
                  BlockCfg(id="kv_end", kind="vacuum", duration_s=0.5, periodic_rate=4000)]
    return cfg


@pytest.fixture(scope="session")
def std_run(tmp_path_factory):
    d = tmp_path_factory.mktemp("std")
    res = run(small_standard(), d)
    return res, d


@pytest.fixture(scope="session")
def phase_run(tmp_path_factory):
    d = tmp_path_factory.mktemp("phase")
    cfg = SimConfig.load(ROOT / "config" / "phase_coherent.yaml")
    cfg.run.seed = 5
    res = run(cfg, d)
    return res, d


@pytest.fixture(scope="session")
def free_run(tmp_path_factory):
    d = tmp_path_factory.mktemp("free")
    cfg = SimConfig.load(ROOT / "config" / "free_clock.yaml")
    cfg.run.seed = 3
    cfg.blocks = [BlockCfg(id="kv", kind="vacuum", duration_s=0.3, periodic_rate=2000),
                  BlockCfg(id="heralded", kind="heralded", duration_s=2.0)]
    res = run(cfg, d)
    return res, d
