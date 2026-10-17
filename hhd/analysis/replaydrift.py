"""Drift-Überwachung über eine Wiedergabe (lange Sitzungen, beschränkter Speicher).

Die Sitzung wird mit ``hhd.server.devices.ReplayBackend`` (ohne Echtzeit) in Zeitfenstern von ``chunk_s``
gelesen; jedes Fenster geht in ``hhd.analysis.driftmon.DriftMonitor`` (LO aus Kanal B, Vakuumvarianz, η).
Weder Wiedergabe noch Monitor halten mehr als ein Fenster, der Speicher hängt also nicht von der
Sitzungsdauer ab. Δ_T (Herald → Mode) kommt aus ``sync/sync.json``, falls vorhanden, sonst ``delta_T_s``.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from .driftmon import DriftMonitor


def drift_over_replay(path, chunk_s: float = 1.0, window_s: float = 5.0, delta_T_s: float | None = None,
                      gamma: float = 1 / 40e-9, lo_warn_rel: float = 0.01, z_warn: float = 4.0,
                      progress=None) -> dict:
    from ..server.devices import ReplayBackend
    path = Path(path)
    t_wall = time.time()
    dT = delta_T_s
    src = "Parameter" if dT is not None else "Standard"
    sj = path / "sync" / "sync.json"
    if dT is None and sj.exists():
        d = json.loads(sj.read_text(encoding="utf-8")).get("delta", {})
        if d.get("available") and np.isfinite(d.get("delta_T_s", np.nan)):
            dT, src = float(d["delta_T_s"]), "sync.json"
    be = ReplayBackend(path, chunk_s=chunk_s, realtime=False)
    be.kinds = {"digitizer"}
    specs = be.prepare()
    fs = float(be.session.runs[0].fs) if be.session.runs else 2.5e9
    kw = dict(fs=fs, gamma=gamma, window_s=window_s, lo_warn_rel=lo_warn_rel, z_warn=z_warn)
    if dT is not None:
        kw["delta_T_s"] = dT
    mon = DriftMonitor(**kw)
    t_first = min((r.t_start_s for r in be.session.runs), default=0.0)
    n_rec = 0
    for i, spec in enumerate(specs):
        run = be.session.runs[i]
        t_run = float(run.t_start_s) - t_first
        be.start_run(i)
        mon.begin_run(spec.block_id, spec.kind, t_run)
        k = 0
        while True:
            d = be.read("digitizer")
            if d is None:
                break
            k += 1
            if len(d.data):
                mon.feed(d.data, d.trig_D, d.first_D, d.data_b, t_run + k * chunk_s)
                n_rec += len(d.data)
            if d.run_done:
                break
        mon.end_run()
        if progress:
            progress(i + 1, len(specs))
    if be._run is not None:
        be._run.release()
    out = mon.summary()
    out.update(n_records=n_rec, chunk_s=chunk_s, delta_T_s=mon.dT, delta_T_source=src,
               time_s=round(time.time() - t_wall, 2))
    return out


def render_drift(d: dict) -> str:
    L = [f"Drift über Wiedergabe: {len(d['runs'])} Läufe, {d['n_records']} Records, {d['time_s']} s "
         f"(Fenster {d['chunk_s']} s, Δ_T {1e9 * d['delta_T_s']:.1f} ns aus {d['delta_T_source']})"]
    for r in d["runs"]:
        L.append(f"  {r['block_id']:<10} {r['kind']:<10} t={r['t_start_s']:8.1f} s  LO {_p(r.get('lo_rel')) if r['kind'] != 'electronic' else '(dunkel)'}  "
                 f"n={r.get('n', 0):6d}  var={_f(r.get('var'))}  η_rel {_f(r.get('eta_rel'))}")
    tr = d.get("trend", {})
    if "lo" in tr:
        L.append(f"Trend LO: {_f(tr['lo'].get('change_over_span'))} über {tr['lo'].get('span_s', 0):.0f} s "
                 f"(z = {_f(tr['lo'].get('z'))})")
    if "eta" in tr:
        L.append(f"Trend η: {_f(tr['eta'].get('change_over_span'))} über {tr['eta'].get('span_s', 0):.0f} s "
                 f"(z = {_f(tr['eta'].get('z'))})")
    for w in d.get("warnings", []):
        L.append(f"WARNUNG: {w}")
    return "\n".join(L)


def _f(x) -> str:
    return "–" if x is None or not np.isfinite(x) else f"{x:+.4f}"


def _p(x) -> str:
    return "–" if x is None or not np.isfinite(x) else f"{100 * (x - 1):+.2f} %"
