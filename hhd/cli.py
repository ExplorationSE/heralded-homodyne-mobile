"""Kommandozeile ``hhd`` (Auswertung): Import, Synchronisation, Validierung, Datenstrom, Leistungsmessung.

    hhd import   <sitzung|kampagne>          Import prüfen, Übersicht der Läufe
    hhd sync     <sitzung|kampagne> [-o DIR] Synchronisation/Zuordnung/Δ → sync/sync.h5 + sync/sync.json
    hhd validate <sitzung|kampagne>          (nur Simulation) Vergleich mit truth.h5
    hhd stream   <sitzung> [--run ID]        chunkweiser Synchronisierer, Vergleich mit Batch
    hhd bench    <sitzung>                   Durchsatz (Ereignisse/s)
    hhd calibrate   <sitzung>                Quadraturen, Vakuum-/Elektronik-/LO-Kalibrierung, Drift, Übersprechen, PCA
    hhd reconstruct <sitzung>                zusätzlich phasengemittelte Rekonstruktion mit Bootstrap
    hhd compare     <sitzung>                vollständige Kette inkl. Vergleich A/B → analysis/result.json
    hhd report      <sitzung|result.json>    Bericht (Markdown) ausgeben
    hhd validate-analysis <sitzung>          (nur Simulation) Auswertung gegen truth.h5

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
    if getattr(a, "delay_method", None):
        p.delay_method = a.delay_method
    if getattr(a, "chunked_sync", None):
        p.chunked = a.chunked_sync
    if getattr(a, "segment_s", None):
        p.segment_s = a.segment_s
    if getattr(a, "chunked_min_s", None):
        p.chunked_min_s = a.chunked_min_s
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
    prm = _params(a)
    prm.chunked = "never"                       # Vergleich mit der Wahrheit braucht die Batch-Objekte je Lauf
    v = validate_any(a.path, prm)
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


def cmd_drift(a) -> int:
    from .analysis.replaydrift import drift_over_replay, render_drift
    d = drift_over_replay(a.path, chunk_s=a.chunk_s, window_s=a.window_s,
                          delta_T_s=a.delta_t_ns * 1e-9 if a.delta_t_ns else None)
    print(json.dumps(d, indent=1, ensure_ascii=False, default=float) if a.json else render_drift(d))
    return int(bool(d["warnings"]) and a.strict)


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


def _aparams(a):
    from .analysis.pipeline import AnalysisParams
    ap = AnalysisParams()
    for k in ("norm", "estimator", "nmax", "n_boot", "seed", "mode_source", "dark_count_rate"):
        v = getattr(a, k, None)
        if v is not None:
            setattr(ap, k, v)
    if a.clearance_db is not None:
        ap.clearance_dB = a.clearance_db
    if a.gamma_ns:
        ap.gamma = 1.0 / (a.gamma_ns * 1e-9)
    if a.mode_shape:
        ap.mode_shape = a.mode_shape
    ap.boot_vacuum = not a.no_boot_vacuum
    ap.pca = not a.no_pca or ap.mode_source in ("pca", "pca_raw")
    if getattr(a, "pca_max_records", None) is not None:
        ap.pca_max_records = int(a.pca_max_records)
    ap.bandwidth_model = not a.no_bandwidth
    return ap


def _run_analysis(a, stages):
    from .analysis.pipeline import analyze_session
    from .io.session import is_campaign
    from pathlib import Path
    paths = sorted(p.parent for p in Path(a.path).glob("*/session.json")) if is_campaign(a.path) else [Path(a.path)]
    out = []
    for p in paths:
        if getattr(a, "streaming", False):
            from .analysis.streaming import analyze_session_streaming
            if stages != ("reconstruct", "compare"):
                print("Hinweis: --streaming führt stets die vollständige Kette aus", file=sys.stderr)
            r = analyze_session_streaming(p, _aparams(a), out_dir=a.out if len(paths) == 1 else None,
                                          sync_params=_params(a), chunk=a.chunk)
        else:
            r = analyze_session(p, _aparams(a), out_dir=a.out if len(paths) == 1 else None,
                                sync_params=_params(a), stages=stages)
        out.append(r)
    return out


def cmd_calibrate(a) -> int:
    from .analysis.report import render_report
    for r in _run_analysis(a, ()):
        print(render_report(r) if not a.json else json.dumps(r["calibration"], indent=1, ensure_ascii=False, default=float))
        print(f"Ausgabe: {r['_out_dir']}")
    return 0


def cmd_reconstruct(a) -> int:
    from .analysis.report import render_report
    for r in _run_analysis(a, ("reconstruct",)):
        print(render_report(r)); print(f"Ausgabe: {r['_out_dir']}")
    return 0


def cmd_compare(a) -> int:
    from .analysis.report import render_report
    rc = 0
    for r in _run_analysis(a, ("reconstruct", "compare")):
        print(render_report(r)); print(f"Ausgabe: {r['_out_dir']}")
        rc |= bool(r["warnings"]) and a.strict
    return int(rc)


def cmd_report(a) -> int:
    from pathlib import Path
    from .analysis.report import report_from_file
    p = Path(a.path)
    f = p if p.suffix == ".json" else p / "analysis" / "result.json"
    if not f.exists():
        print(f"{f} fehlt – zuerst `hhd compare {a.path}` ausführen", file=sys.stderr)
        return 2
    txt = report_from_file(f)
    if a.out:
        Path(a.out).write_text(txt, encoding="utf-8"); print(f"Bericht: {a.out}")
    else:
        print(txt)
    return 0


def cmd_validate_records(a) -> int:
    from .sim.record_validate import validate_records
    v = validate_records(a.path, a.sync_dir)
    if a.json:
        print(json.dumps(v, indent=1, ensure_ascii=False, default=float)); return 0
    sm = v["summary"]
    print(f"Validierung je Record {a.path}")
    for k, r in v["runs"].items():
        if "error" in r:
            print(f"  {k:10s} {r['error']}"); continue
        print(f"  {k:10s} Records {r['n_records']:9d}  zugeordnet {r['n_matched']:9d}  Fehlzuordnungen {r['misassigned']}"
              f"  verloren {r['lost']}/{r['herald_records']}")
    print(f"  Summe: Fehlzuordnungen {sm['misassigned']}/{sm['n_matched']} (Rate {sm['misassignment_rate']:.2e}), "
          f"verloren {sm['lost']}/{sm['herald_records']}, Effizienz {sm['efficiency']:.4%}")
    return 0 if sm["misassignment_rate"] < 1e-3 else 1


def cmd_validate_analysis(a) -> int:
    from pathlib import Path
    from .analysis.pipeline import analyze_session
    from .sim.analysis_validate import validate_analysis
    p = Path(a.path)
    f = p / "analysis" / "result.json"
    r = json.loads(f.read_text(encoding="utf-8")) if (f.exists() and not a.rerun) else \
        analyze_session(p, _aparams(a), sync_params=_params(a))
    v = validate_analysis(p, r)
    if a.json:
        print(json.dumps(v, indent=1, ensure_ascii=False, default=float)); return 0
    print(f"Validierung Auswertung {v['session_id']}")
    for k, g in v["groups"].items():
        for q in ("var_x", "W00", "W00_bc", "W00_moments", "eta_fit"):
            o = g.get(q)
            if o and o.get("err") and o["err"] > 1e-6:          # Vakuum am Rand (W = 1/π): ohne Pull
                print(f"  {k:3s} {q:12s} {o['value']:+.4f} ± {o['err']:.4f}  wahr {o['true']:+.4f}  Pull {o['pull']:+.2f}")
    for q in ("eta_comparison", "W00_diff", "eta_state"):
        o = v.get(q)
        if o and o.get("pull") is not None:
            print(f"  {q:16s} {o['value']:+.4f} ± {o['err']:.4f}  wahr {o['true']:+.4f}  Pull {o['pull']:+.2f}")
    for q in ("eta_el", "xi"):
        if q in v:
            print(f"  {q:16s} {v[q]['value']:.4f}  wahr {v[q]['true']:.4f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["check-devices"]:            # Optionen unverändert an hhd.server.check weiterreichen
        from .server.check import main as check_main
        return check_main(argv[1:])
    ap = argparse.ArgumentParser(prog="hhd", description="Heralded Homodyne Mobile – Auswertung (Import, Synchronisation, Kalibrierung, Rekonstruktion, Vergleich)")
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
        p.add_argument("--delay-method", choices=("stream", "matrix"),
                       help="Δ-Akkumulation: stream (Standard, Speicher unabhängig von der Recordzahl) | matrix")
        p.add_argument("--chunked-sync", choices=("auto", "always", "never"),
                       help="Läufe in Segmenten synchronisieren (beschränkter Speicher; auto: ab --chunked-min-s)")
        p.add_argument("--segment-s", type=float, help="Segmentlänge der segmentierten Synchronisation (Standard 30 s)")
        p.add_argument("--chunked-min-s", type=float, help="auto: Läufe ab dieser Dauer segmentiert (Standard 600 s)")

    p = sp.add_parser("import", help="Import prüfen und Übersicht ausgeben"); p.add_argument("path")
    p.set_defaults(f=cmd_import)
    p = sp.add_parser("sync", help="Synchronisation/Zuordnung, Ausgabe sync/sync.h5 und sync/sync.json"); common(p)
    p.add_argument("-o", "--out", help="Ausgabeordner (Standard <sitzung>/sync)")
    p.add_argument("--strict", action="store_true", help="Rückgabewert 1 bei Warnungen")
    p.set_defaults(f=cmd_sync)
    p = sp.add_parser("validate", help="Vergleich mit der Simulator-Wahrheit (nur Simulation)"); common(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(f=cmd_validate)
    p = sp.add_parser("validate-records", help="Fehlzuordnungen gegen truth_records.h5 (Datenstrom-Simulator, lange Läufe)")
    p.add_argument("path"); p.add_argument("--sync-dir"); p.add_argument("--json", action="store_true")
    p.set_defaults(f=cmd_validate_records)
    p = sp.add_parser("stream", help="chunkweiser Synchronisierer gegen Batch"); p.add_argument("path")
    p.add_argument("--run"); p.add_argument("--chunk", type=int, default=1024)
    p.set_defaults(f=cmd_stream)
    p = sp.add_parser("drift", help="Drift (LO, Vakuumvarianz, η) über eine Wiedergabe, beschränkter Speicher")
    p.add_argument("path"); p.add_argument("--chunk-s", type=float, default=1.0)
    p.add_argument("--window-s", type=float, default=5.0); p.add_argument("--delta-t-ns", type=float)
    p.add_argument("--json", action="store_true"); p.add_argument("--strict", action="store_true")
    p.set_defaults(f=cmd_drift)

    p = sp.add_parser("bench", help="Durchsatz messen"); p.add_argument("path")
    p.add_argument("--repeat", type=int, default=1); p.add_argument("--no-delay", action="store_true")
    p.set_defaults(f=cmd_bench)

    def anal(p):
        common(p)
        p.add_argument("-o", "--out", help="Ausgabeordner (Standard <sitzung>/analysis)")
        p.add_argument("--norm", choices=("pooled", "interpolated", "monitor", "monitor_record"))
        p.add_argument("--clearance-db", type=float, help="Clearance (dB) aus Labormessung, falls kein Dunkellauf")
        p.add_argument("--estimator", choices=("maxlik", "em", "binned", "moments"))
        p.add_argument("--nmax", type=int); p.add_argument("--n-boot", type=int); p.add_argument("--seed", type=int)
        p.add_argument("--mode-source", choices=("parametric", "pca", "pca_raw"))
        p.add_argument("--pca-max-records", type=int, default=None,
                       help="PCA: höchstens so viele Fenster je Gruppe (Standard 20000, 0 = alle)")
        p.add_argument("--no-pca", action="store_true"); p.add_argument("--no-bandwidth", action="store_true")
        p.add_argument("--no-boot-vacuum", action="store_true", help="Vakuumnormierung im Bootstrap festhalten (alt)")
        p.add_argument("--dark-count-rate", type=float, help="Herald-Dunkelzählrate 1/s für ξ (Standard 100)")
        p.add_argument("--json", action="store_true"); p.add_argument("--strict", action="store_true")
        p.add_argument("--streaming", action="store_true",
                       help="Datenstrom-Auswertung mit beschränktem Speicher (lange Sitzungen)")
        p.add_argument("--chunk", type=int, default=1024, help="Records je Block bei --streaming")

    p = sp.add_parser("calibrate", help="Quadraturen und Kalibrierung"); anal(p); p.set_defaults(f=cmd_calibrate)
    p = sp.add_parser("reconstruct", help="Kalibrierung + Rekonstruktion"); anal(p); p.set_defaults(f=cmd_reconstruct)
    p = sp.add_parser("compare", help="vollständige Kette inkl. Vergleich A/B"); anal(p); p.set_defaults(f=cmd_compare)
    p = sp.add_parser("report", help="Bericht aus analysis/result.json"); p.add_argument("path")
    p.add_argument("-o", "--out"); p.set_defaults(f=cmd_report)
    p = sp.add_parser("validate-analysis", help="(nur Simulation) Auswertung gegen truth.h5"); anal(p)
    p.add_argument("--rerun", action="store_true"); p.set_defaults(f=cmd_validate_analysis)
    sp.add_parser("check-devices", help="Geräteprüfung ohne Messung (wie python -m hhd.server.check; "
                  "Optionen: hhd check-devices --help)")
    a = ap.parse_args(argv)
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())
