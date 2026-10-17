"""Lesen der Synchronisationsergebnisse (``sync/sync.h5`` + ``sync/sync.json``) und Modenpositionen je Record."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import h5py
import numpy as np

from ..sync import assign as A


@dataclass
class RunSyncData:
    block: str
    seg_class: np.ndarray
    seg_herald: np.ndarray | None
    seg_n_secondary: np.ndarray | None
    seg_overlap_prev: np.ndarray | None
    herald_D: np.ndarray | None
    record_time_session: np.ndarray
    report: dict
    seg_n_heralds: np.ndarray | None = None


@dataclass
class SyncData:
    path: Path
    report: dict
    runs: dict[str, RunSyncData] = field(default_factory=dict)

    @property
    def delta_T(self) -> float:
        d = self.report.get("delta", {})
        return float(d["delta_T_s"]) if d.get("available") else float("nan")

    def delta_run(self, block: str) -> float:
        d = self.report.get("delta", {})
        pr = d.get("per_run", {}).get(block)
        return float(pr["delta_s"]) if pr else float("nan")


def load_sync(sync_dir: str | Path) -> SyncData:
    d = Path(sync_dir)
    rep = json.loads((d / "sync.json").read_text(encoding="utf-8"))
    sd = SyncData(d, rep)
    with h5py.File(d / "sync.h5", "r") as f:
        for k, g in f["runs"].items():
            get = lambda n: g[n][:] if n in g else None
            sd.runs[k] = RunSyncData(k, g["seg_class"][:], get("seg_herald"), get("seg_n_secondary"),
                                     get("seg_overlap_prev"), get("herald_D_s"), g["record_time_session_s"][:],
                                     rep["runs"][k], get("seg_n_heralds"))
    return sd


def ensure_sync(session, params=None, force: bool = False) -> SyncData:
    """Vorhandene Synchronisation lesen bzw. erzeugen (``<sitzung>/sync``)."""
    out = Path(session.path) / "sync"
    if force or not (out / "sync.h5").exists() or not (out / "sync.json").exists():
        from ..sync.pipeline import sync_session
        sync_session(session, params, out)
    return load_sync(out)


def mode_centres(run, rsd: RunSyncData, sd: SyncData) -> tuple[np.ndarray, np.ndarray]:
    """Modenzentrum je Record (Digitizer-Zeit) und Maske „sauber“ (zugeordnet, keine Fremdmode, keine Überlappung)."""
    trig = run.trigger_D()
    dT = sd.delta_T
    if not np.isfinite(dT):
        raise ValueError("keine Δ-Kalibrierung in sync.json (hhd sync ohne --no-delay ausführen)")
    c = trig + dT
    clean = np.ones(run.n_records, bool)
    if rsd.seg_herald is not None and rsd.herald_D is not None:
        m = rsd.seg_herald >= 0
        dr = sd.delta_run(run.block_id)
        if np.isfinite(dr):
            c[m] = rsd.herald_D[rsd.seg_herald[m]] + dr
        clean = m & (rsd.seg_n_secondary == 0) & ~rsd.seg_overlap_prev
        if run.dataset_type != "heralded" and run.dataset_type != "herald_blocked":
            clean = np.ones(run.n_records, bool)
    if rsd.seg_n_heralds is not None:              # Lauf B/K-V: Records mit fremder Mode im Fenster markieren
        pass
    return c, clean


def group_mask(run, rsd: RunSyncData) -> dict[str, np.ndarray]:
    """Auswertegruppen je Lauf: Hauptgruppe nach Laufart, eingestreute Zufallstrigger getrennt."""
    sc = rsd.seg_class
    dt = run.dataset_type
    g = {}
    if dt in ("heralded", "herald_blocked"):
        g["main"] = sc == A.SEG_MATCHED
        if np.any(sc == A.SEG_RANDOM):
            g["random"] = sc == A.SEG_RANDOM
    elif dt == "random_trigger":
        g["main"] = sc == A.SEG_RANDOM
    elif dt == "state":
        g["main"] = sc == A.SEG_STATE
    else:
        g["main"] = np.ones(len(sc), bool)
    return g
