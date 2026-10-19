"""Fehlzuordnungen gegen die Zuordnungswahrheit je Record (``truth_records.h5`` des Datenstrom-Simulators).

Für Sitzungen über Stunden, bei denen ``hhd validate`` (Batch-Neusynchronisation gegen ``truth.h5`` mit allen
Ereignissen) zu viel Speicher bräuchte: verglichen wird die vorhandene ``sync/sync.h5`` (auch segmentiert
synchronisiert) blockweise mit ``truth_records.h5``. Speicher (0.14): die PTU-Datei wird in Blöcken dekodiert
(``RunData.iter_harp``), gehalten werden nur die Herald-Zeiten eines Laufs (8 B je Herald) plus ein Block von
``block`` Records – nicht mehr alle Ereignisse des Laufs.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import h5py
import numpy as np

from ..io.session import load_session
from ..sync import assign as A
from .digitizer import TRIG_HERALD
from .streaming import RecordTruthWriter


def herald_ps_stream(run, chunk_records: int = 1 << 18) -> np.ndarray:
    """Herald-Zeiten (ps, sortiert) eines Laufs, PTU blockweise dekodiert."""
    parts = [hs.role_ps("herald").astype(np.int64, copy=False) for hs in run.iter_harp(chunk_records)]
    parts = [p for p in parts if len(p)]
    if not parts:
        return np.empty(0, np.int64)
    h = np.concatenate(parts)
    if len(h) > 1 and np.any(h[1:] < h[:-1]):
        h.sort()
    return h


def validate_records(path: str | Path, sync_dir: str | Path | None = None, block: int = 1 << 16,
                     chunk_records: int = 1 << 18) -> dict:
    path = Path(path)
    sd = Path(sync_dir) if sync_dir else path / "sync"
    tf = path / RecordTruthWriter.FILE
    if not tf.exists():
        raise FileNotFoundError(f"{tf} fehlt (Datenstrom-Simulator mit record_truth / sim_record_truth = true)")
    t0 = time.time()
    sess = load_session(path, lazy=True)
    out: dict = dict(session=str(path), runs={})
    tot = dict(n_records=0, herald_records=0, n_matched=0, misassigned=0, lost=0, lost_ambiguous=0,
               matched_dark=0, matched_no_truth=0)
    with h5py.File(tf, "r") as T, h5py.File(sd / "sync.h5", "r") as S:
        for run in sess.runs:
            bid = run.block_id
            if f"blocks/{bid}" not in T or f"runs/{bid}" not in S or "seg_herald" not in S[f"runs/{bid}"]:
                continue
            tg, sg = T[f"blocks/{bid}"], S[f"runs/{bid}"]
            n = tg["kind"].shape[0]
            if sg["seg_herald"].shape[0] != n:
                out["runs"][bid] = dict(error=f"Recordzahl {sg['seg_herald'].shape[0]} ≠ Wahrheit {n}")
                continue
            ours = herald_ps_stream(run, chunk_records)
            r = dict(n_records=0, herald_records=0, n_matched=0, misassigned=0, lost=0, lost_ambiguous=0,
                     matched_dark=0, matched_no_truth=0, complete=bool(tg.attrs.get("complete", False)))
            for s in range(0, n, block):
                kind = tg["kind"][s:s + block]; hps = tg["herald_ps"][s:s + block]; dark = tg["is_dark"][s:s + block]
                sh = sg["seg_herald"][s:s + block]; cls = sg["seg_class"][s:s + block]
                matched = sh >= 0
                our_ps = np.where(matched, ours[np.clip(sh, 0, max(len(ours) - 1, 0))] if len(ours) else -1, -1)
                should = (kind == TRIG_HERALD) & (hps >= 0)
                mis = matched & (our_ps != hps)
                r["n_records"] += len(kind); r["herald_records"] += int(should.sum())
                r["n_matched"] += int(matched.sum()); r["misassigned"] += int(mis.sum())
                r["lost"] += int(np.sum(should & ~matched))
                r["lost_ambiguous"] += int(np.sum(should & ~matched & (cls == A.SEG_AMBIGUOUS)))
                r["matched_dark"] += int(np.sum(matched & ~mis & dark))
                r["matched_no_truth"] += int(np.sum(matched & (hps < 0)))
            r["misassignment_rate"] = r["misassigned"] / max(r["n_matched"], 1)
            r["efficiency"] = (r["n_matched"] - r["misassigned"]) / max(r["herald_records"], 1)
            out["runs"][bid] = r
            for k in tot:
                tot[k] += r[k]
            if hasattr(run, "release"):
                run.release()
    tot["misassignment_rate"] = tot["misassigned"] / max(tot["n_matched"], 1)
    tot["efficiency"] = (tot["n_matched"] - tot["misassigned"]) / max(tot["herald_records"], 1)
    sj = sd / "sync.json"
    if sj.exists():
        j = json.loads(sj.read_text(encoding="utf-8"))
        tot["chunked_runs"] = [k for k, v in j.get("runs", {}).items() if isinstance(v, dict) and v.get("chunked")]
    tot["time_s"] = round(time.time() - t0, 2)
    out["summary"] = tot
    return out
