"""Speicherbedarf langer Sitzungen messen (Wiedergabe, Synchronisation, Auswertung im Speicher/als Datenstrom, Drift).

    PYTHONPATH=. python tools/longrun_memory.py SESSION [SESSION ...] [--gen SCALE:OUT ...] [--n-boot 100]

``--gen 4:/tmp/x4`` erzeugt vorher eine Sitzung mit dem Datenstrom-Simulator (Standard-Laufplan, Laufdauern ×4,
L = 1000). Jede Stufe läuft in einem eigenen Prozess; ausgegeben werden Zeit, tracemalloc-Spitze (Python/numpy)
und maximale RSS je Stufe.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

STAGE = r'''
import json, resource, sys, time, tracemalloc
from pathlib import Path
stage, path, nb = sys.argv[1], sys.argv[2], int(sys.argv[3])
tracemalloc.start(); t = time.time(); info = {}
if stage == "gen":
    from hhd.sim.config import SimConfig
    from hhd.sim.streaming import StreamingSimulator
    scale = float(sys.argv[4])
    cfg = SimConfig.load("config/standard.yaml")
    cfg.digitizer.record_length, cfg.digitizer.pretrigger = 1000, 400
    for b in cfg.blocks:
        b.duration_s = round(b.duration_s * scale / 0.25) * 0.25 or 0.25
    cfg.session.id = f"LONG-x{scale:g}"
    man = StreamingSimulator(cfg, slice_s=0.25).run(path)
    info["records"] = sum(r["n_records"] for r in man["runs"])
elif stage == "sync":
    from hhd.sync.pipeline import sync_session
    sync_session(path, None, Path(path) / "sync")
elif stage == "replay":
    from hhd.server.devices import ReplayBackend
    be = ReplayBackend(path, chunk_s=0.1, realtime=False); n = 0
    for i, _ in enumerate(be.prepare()):
        be.start_run(i)
        while True:
            h, d = be.read("harp"), be.read("digitizer")
            if h is None and d is None:
                break
            n += len(d.data) if d is not None else 0
    info["records"] = n
elif stage in ("memory", "streaming"):
    from hhd.analysis.pipeline import AnalysisParams, analyze_session
    from hhd.analysis.streaming import analyze_session_streaming
    f = analyze_session if stage == "memory" else analyze_session_streaming
    r = f(path, AnalysisParams(n_boot=nb), out_dir=Path(path) / f"analysis_{stage}")
    a = r["reconstruction"]["A"]
    info = {k: round(a[k], 5) for k in ("var_x", "var_x_err", "W00", "W00_err", "eta_fit", "eta_fit_err")}
    info["n_A"] = a["n"]
elif stage == "drift":
    from hhd.analysis.replaydrift import drift_over_replay
    d = drift_over_replay(path)
    info = dict(records=d["n_records"], lo_z=round(d["trend"]["lo"]["z"], 2), warnings=len(d["warnings"]))
print(json.dumps(dict(stage=stage, time_s=round(time.time() - t, 1),
                      peak_tm_MB=round(tracemalloc.get_traced_memory()[1] / 1e6, 1),
                      maxrss_MB=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024), **info)))
'''


def run(stage, path, nb, *extra):
    out = subprocess.run([sys.executable, "-c", STAGE, stage, str(path), str(nb), *extra], capture_output=True,
                         text=True, check=True, env=None)
    return json.loads(out.stdout.strip().splitlines()[-1])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("sessions", nargs="*")
    ap.add_argument("--gen", action="append", default=[], help="SCALE:OUT")
    ap.add_argument("--n-boot", type=int, default=100)
    ap.add_argument("--stages", default="replay,memory,streaming,drift")
    a = ap.parse_args(argv)
    paths = [Path(p) for p in a.sessions]
    rows = []
    for g in a.gen:
        sc, out = g.split(":", 1)
        rows.append(dict(session=out, **run("gen", out, 0, sc)))
        paths.append(Path(out))
    for p in paths:
        if not (p / "sync" / "sync.json").exists():
            rows.append(dict(session=str(p), **run("sync", p, 0)))
        for st in a.stages.split(","):
            rows.append(dict(session=str(p), **run(st, p, a.n_boot)))
            print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    return rows


if __name__ == "__main__":
    main()
