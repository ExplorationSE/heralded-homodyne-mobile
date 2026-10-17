"""Kommandozeile ``hhd`` (Auswertung): Import, Synchronisation, Validierung, Datenstrom, Leistungsmessung.

    hhd import   <sitzung|kampagne>          Import prüfen, Übersicht der Läufe
    hhd sync     <sitzung|kampagne> [-o DIR] Synchronisation/Zuordnung/Δ → sync/sync.h5 + sync/sync.json
    hhd validate <sitzung|kampagne>          (nur Simulation) Vergleich mit truth.h5
    hhd stream   <sitzung> [--run ID]        chunkweiser Synchronisierer, Vergleich mit Batch
    hhd bench    <sitzung>                   Durchsatz (Ereignisse/s)

Der Simulator bleibt ``hhd-sim`` (bzw. ``python -m hhd.sim``).
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import numpy as np

from . import __version__


def _fmt_s(x) -> str:
    if x is None or not np.isfinite(x):
        return "–"
    a = abs(x)
    if a >= 1e-6:
        return f"{x * 1e6:.3f} µs"
    if a >= 1e-9:
        return f"{x * 1e9:.3f} ns"
    return f"{x * 1e12:.2f} ps"


def _params(a):
    from .sync.pipeline import SyncParams
    p = SyncParams()
    if a.gamma_ns:
        p.gamma = 1.0 / (a.gamma_ns * 1e-9)
    if a.mode_shape:
        p.mode_shape = a.mode_shape
    if a.no_delay:
        p.calibrate_delay = False
    if a.no_refine:
        p.refine = False
    if a.linear:
        p.allow_piecewise = False
    if a.delay_records:
        p.delay_max_records = a.delay_records
    return p


def _print_sync(rep: dict) -> None:
    q = rep["quality"]
    print(f"Sitzung {rep['session_id']}  ({rep['software']}, Kern {rep['backend']})")
    print(f"{'Block':12s} {'Art':15s} {'Paare':>6s} {'u':>5s} {'Modell':10s} {'σ_r':>10s} {'σ_Paar':>10s} "
          f"{'zugeord.':>8s} {'verw.':>5s} {'mehrd.':>6s} {'sek.':>4s} {'Totz.':>5s} {'CH2':>6s}")
    for k, r in rep["runs"].items():
        a = r.get("assignment") or {}
        seg = a.get("segments", {}); her = a.get("heralds", {}); rea = a.get("reasons", {})
        cc = r["copy_check"]
        print(f"{k:12s} {r['dataset_type']:15s} {r['marker']['n_pairs']:6d} {r['marker']['uniqueness']:5.2f} "
              f"{r['clock']['model']:10s} {_fmt_s(r['clock']['sigma_r']):>10s} {_fmt_s(r['refine'].get('sigma_after_s')):>10s} "
              f"{seg.get('zugeordnet', '–'):>8} "
              f"{seg.get('verwaist', '–'):>5} {seg.get('mehrdeutig', '–'):>6} {her.get('sekundär', '–'):>4} "
              f"{rea.get('Totzeit', '–'):>5} {cc.get('fraction_confirmed', float('nan')):6.1%}")
    d = rep["delta"]
    if d.get("available"):
        print(f"Δ_T (Mode − Trigger) = {_fmt_s(d['delta_T_s'])} ± {_fmt_s(d['sigma_delta_T_s'])}  "
              f"(Kontrast A/c = {d['contrast']:.2f}, {d['n_records']} Records, Normierung {d['norm']['source']})")
        for k, v in d["per_run"].items():
            print(f"   {k:12s} Δ (Mode − Herald) = {_fmt_s(v['delta_s'])} ± {_fmt_s(v['sigma_s'])}")
    else:
        print(f"Δ: nicht bestimmt ({d.get('reason', '')})")
    pf = rep["performance"]
    print(f"Laufzeit {pf['elapsed_s']:.2f} s, {pf['n_events']} Ereignisse, {pf['events_per_s']:.3g} Ereignisse/s")
    print(f"Selbstprüfung: {q['status']} ({q['n_warnings']} Warnungen)")
    for w in q["warnings"]:
        print("  ! " + w)


def cmd_import(a) -> int:
    from .io.session import is_campaign, load_campaign, load_session
    sess = load_campaign(a.path).sessions if is_campaign(a.path) else [load_session(a.path)]
    rc = 0
    for s in sess:
        sm = s.summary()
        print(f"Sitzung {sm['session_id']} (Kampagne {sm['campaign_id']}, Kalibrierung {sm['calibration_id']})")
        for r in s.runs:
            x = r.summary()
            print(f"  {x['block']:12s} {x['dataset_type']:15s} {x['duration_s']:7.3f} s  Records {x['n_records']:6d}  "
                  f"SYNC {x['n_sync_D']:5d}  Harp " + ", ".join(f"{k} {v}" for k, v in x["harp"].items())
                  + ("  Kanal B" if x["channel_b"] else ""))
        for w in s.warnings:
            print("  ! " + w); rc = 1
    return rc


def cmd_sync(a) -> int:
    from .sync import backend
    from .sync.pipeline import sync_any
    backend()
    rep = sync_any(a.path, _params(a), a.out)
    if "sessions" in rep:
        for r in rep["sessions"].values():
            _print_sync(r); print()
        print(f"Kampagne: {rep['summary']}")
        bad = rep["summary"]["n_warnings"]
    else:
        rep.pop("_syncs", None)
        _print_sync(rep)
        print(f"Ausgabe: {rep['output']}")
        bad = rep["quality"]["n_warnings"]
    return 1 if (bad and a.strict) else 0


def cmd_validate(a) -> int:
    from .sim.sync_validate import validate_any
    v = validate_any(a.path, _params(a))
    if a.json:
        print(json.dumps({k: v[k] for k in ("summary",)}, indent=1, ensure_ascii=False, default=float))
    sessions = v["sessions"] if "sessions" in v else {"": v}
    ok = True
    for sid, s in sessions.items():
        sm = s["summary"]
        print(f"Validierung {sid or s['session']}")
        print(f"  Markerpaare korrekt   {sm['marker_correct']}/{sm['marker_pairs']}")
        print(f"  Fehlzuordnungen       {sm['misassigned']}/{sm['n_matched']} (Rate {sm['misassignment_rate']:.2e}), "
              f"verloren {sm['lost']}/{sm['herald_records']}, Effizienz {sm['efficiency']:.4%}")
        for nm in ("pull_a", "pull_b", "pull_delta"):
            p = sm[nm]
            if p.get("n"):
                print(f"  {nm:20s}  n={p['n']:3d}  Mittel {p['mean']:+.2f}  Streuung {p['std']:.2f}  max |{p['max_abs']:.2f}|")
        print(f"  Abbildungsfehler max. RMS über Läufe {_fmt_s(sm['mapping_rms_max_s'])}")
        if "delta_T" in sm:
            d = sm["delta_T"]
            print(f"  Δ_T  fit {_fmt_s(d['fit_s'])} ± {_fmt_s(d['sigma_s'])}, wahr {_fmt_s(d['true_s'])}, Pull {d['pull']:+.2f}")
        ok &= sm["misassignment_rate"] < 1e-3 and sm["marker_correct"] == sm["marker_pairs"]
    if "sessions" in v:
        print(f"Kampagne: {v['summary']}")
    return 0 if ok else 1


def cmd_stream(a) -> int:
    from .io.session import load_session
    from .sync.pipeline import SyncParams, sync_session
    from .sync.stream import stream_run
    s = load_session(a.path)
    rep = sync_session(s, SyncParams(calibrate_delay=False), write=False)
    rc = 0
    for k, rs in rep["_syncs"].items():
        if a.run and k != a.run or rs.assignment is None:
            continue
        t = time.perf_counter()
        o = stream_run(rs.run, a.chunk)
        dt = time.perf_counter() - t
        agree = float(np.mean(o["seg_herald"] == rs.assignment.seg_herald)) if len(o["seg_herald"]) == rs.run.n_records else 0.0
        print(f"{k:12s} Übereinstimmung mit Batch {agree:.4%}, Puffer max. {o['max_buffer']}, Refits {o['n_refits']}, {dt * 1e3:.1f} ms")
        rc |= agree < 0.999
    return int(rc)


def cmd_bench(a) -> int:
    from .io.session import load_session
    from .sync.pipeline import SyncParams, sync_session
    s = load_session(a.path)
    for _ in range(a.repeat):
        rep = sync_session(s, SyncParams(calibrate_delay=False), write=False)
    pf = rep["performance"]
    print(f"Synchronisation ohne Δ: {pf['n_events']} Ereignisse in {pf['elapsed_s']:.3f} s → {pf['events_per_s']:.3g} Ereignisse/s")
    if not a.no_delay:
        t = time.perf_counter()
        rep = sync_session(s, SyncParams(), write=False)
        dt = time.perf_counter() - t
        n = rep["delta"].get("n_records", 0)
        print(f"mit Δ-Kalibrierung: {dt:.2f} s gesamt, Δ {rep['delta'].get('time_s', 0):.2f} s für {n} Records "
              f"({n * s.device['record_length'] / max(rep['delta'].get('time_s', 1e-9), 1e-9):.3g} Samples/s je Rasterdurchgang)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hhd", description="ExSE Harp ADQ32 HHD Mobil – Auswertung (Import, Synchronisation)")
    ap.add_argument("--version", action="version", version=f"hhd {__version__}")
    sp = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("path", help="Sitzungsordner (session.json) oder Kampagnenordner (campaign.json)")
        p.add_argument("--gamma-ns", type=float, help="Modenbreite 1/γ in ns (Standard 40)")
        p.add_argument("--mode-shape", choices=("double_exponential", "gauss"))
        p.add_argument("--no-delay", action="store_true", help="ohne Δ-Feinkalibrierung")
        p.add_argument("--no-refine", action="store_true", help="ohne Verfeinerung über Herald/Trigger-Paare")
        p.add_argument("--linear", action="store_true", help="nur lineare Uhrabbildung")
        p.add_argument("--delay-records", type=int, help="max. Records für Δ (Standard 20000)")

    p = sp.add_parser("import", help="Import prüfen und Übersicht ausgeben"); p.add_argument("path")
    p.set_defaults(f=cmd_import)
    p = sp.add_parser("sync", help="Synchronisation/Zuordnung, Ausgabe sync/sync.h5 und sync/sync.json"); common(p)
    p.add_argument("-o", "--out", help="Ausgabeordner (Standard <sitzung>/sync)")
    p.add_argument("--strict", action="store_true", help="Rückgabewert 1 bei Warnungen")
    p.set_defaults(f=cmd_sync)
    p = sp.add_parser("validate", help="Vergleich mit der Simulator-Wahrheit (nur Simulation)"); common(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(f=cmd_validate)
    p = sp.add_parser("stream", help="chunkweiser Synchronisierer gegen Batch"); p.add_argument("path")
    p.add_argument("--run"); p.add_argument("--chunk", type=int, default=1024)
    p.set_defaults(f=cmd_stream)
    p = sp.add_parser("bench", help="Durchsatz messen"); p.add_argument("path")
    p.add_argument("--repeat", type=int, default=1); p.add_argument("--no-delay", action="store_true")
    p.set_defaults(f=cmd_bench)
    a = ap.parse_args(argv)
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())
