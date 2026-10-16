"""Simulator für heralded Homodyn-Messungen (Arbeitspaket AP2): zwei Datenströme
(HydraHarp 500 → PTU, ADQ32-PCIe → HDF5 v1), Wahrheitsdatei, Datenstrom-Modus.

Schnellstart::

    from hhd.sim import SimConfig, simulate, write_run
    cfg = SimConfig.load("config/standard.yaml")
    res = simulate(cfg)
    write_run(res, "out/lauf1")
"""
from .config import BlockCfg, ConfigError, SimConfig, parameter_markdown
from .engine import RunResult, run, simulate, write_run

__all__ = ["SimConfig", "BlockCfg", "ConfigError", "parameter_markdown", "simulate", "write_run", "run", "RunResult"]
