"""Kommandozeile ``hhd-sim`` (bzw. ``python -m hhd.sim``).

Beispiele::

    hhd-sim params --markdown > docs/PARAMETER.md
    hhd-sim config -o meine.yaml                 # Standardkonfiguration schreiben
    hhd-sim run config/standard.yaml -o out/S001          # eine Sitzung (getrennte Läufe, gemeinsame Kalibrierung)
    hhd-sim campaign config/standard.yaml -o out/K1 -n 3  # Kampagne aus 3 Sitzungen
    hhd-sim validate out/S001                    # Schema- und Konsistenzprüfung
    hhd-sim check out/S001 --norm interpolated   # Referenzauswertung, Vergleich A/B, Driftindikatoren
    hhd-sim stream config/standard.yaml --speed 1 --chunk 0.05
    hhd-sim plan --rate 1000 -o config/laufplan_m1.yaml          # Laufplan (Startwerte) mit Statistik/Datenbudget
    hhd-sim plan --allan stabil.csv --rate 10000                 # Anpassungsregel nach der Stabilitätsmessung (AP14)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

from .. import __version__
from ..io.ptu import T2Decoder, read_ptu
from ..io.schema import validate_digitizer_file
from .config import ConfigError, SimConfig, parameter_markdown, parameter_rows


def _load(path: str | None, seed: int | None, scale: float | None) -> SimConfig:
    cfg = SimConfig.load(path) if path else SimConfig()
    if seed is not None:
        cfg.session.seed = seed
    if scale:
        for b in cfg.blocks:
            b.duration_s *= scale
    return cfg


def validate_run(run_dir: str | Path) -> list[str]:
    d = Path(run_dir)
    err = []
    try:
        man = json.loads((d / "session.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ["session.json fehlt"]
    err += validate_digitizer_file(d / "digitizer.h5")
    import hashlib
    for name, info in man["files"].items():
        p = d / name
        if not p.exists():
            err.append(f"{name} fehlt"); continue
        if hashlib.sha256(p.read_bytes()).hexdigest() != info["sha256"]:
            err.append(f"{name}: Prüfsumme weicht ab")
    for b in man["runs"]:
        try:
            f = read_ptu(d / b["ptu_file"])
        except Exception as e:      # noqa: BLE001
            err.append(f"{b['ptu_file']}: {e}"); continue
        ev = T2Decoder().decode(f.records)
        if len(ev.event_time_ps) and (ev.event_time_ps.min() < 0 or np.any(np.diff(ev.event_time_ps) < 0)):
            err.append(f"{b['ptu_file']}: Ereigniszeiten nicht monoton")
        if f.tags.get("UsrHHD_BlockId") != b["id"]:
            err.append(f"{b['ptu_file']}: UsrHHD_BlockId passt nicht")
        if f.tags.get("UsrHHD_RunId") != b["run_id"] or f.tags.get("UsrHHD_SessionId") != man["session_id"]:
            err.append(f"{b['ptu_file']}: Lauf-/Sitzungs-ID im PTU-Kopf passt nicht")
        if f.tags.get("UsrHHD_CalibrationId") != man["calibration"]["calibration_id"]:
            err.append(f"{b['ptu_file']}: calibration_id passt nicht zur Sitzung")
    return err


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hhd-sim", description="Simulator Heralded Homodyne Mobile (HydraHarp 500 + ADQ32)")
    ap.add_argument("--version", action="version", version=f"hhd {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("params", help="Parametertabelle ausgeben")
    p.add_argument("--markdown", action="store_true")
    p = sub.add_parser("config", help="Standardkonfiguration schreiben")
    p.add_argument("-o", "--out", default="-")
    helps = {"run": "Sitzung simulieren und Dateien schreiben", "stream": "Datenstrom abspielen",
             "campaign": "Kampagne aus mehreren Sitzungen simulieren"}
    for name in ("run", "stream", "campaign"):
        p = sub.add_parser(name, help=helps[name])
        p.add_argument("config", nargs="?", help="YAML/JSON (ohne: Standard)")
        p.add_argument("--seed", type=int)
        p.add_argument("--scale-duration", type=float, help="alle Blockdauern multiplizieren")
        if name in ("run", "campaign"):
            p.add_argument("-o", "--out", required=True)
            if name == "campaign":
                p.add_argument("-n", "--sessions", type=int, default=2)
        else:
            p.add_argument("--chunk", type=float, default=0.05)
            p.add_argument("--speed", type=float, default=1.0)
            p.add_argument("--no-realtime", action="store_true")
            p.add_argument("--max-chunks", type=int, default=0)
    p = sub.add_parser("validate", help="Lauf gegen Schema v1 und Prüfsummen prüfen")
    p.add_argument("run_dir")
    p = sub.add_parser("check", help="Referenzauswertung gegen die Sollwerte")
    p.add_argument("run_dir")
    p.add_argument("--json", action="store_true")
    p.add_argument("--boot", type=int, default=100)
    p.add_argument("--norm", choices=("pooled", "interpolated", "monitor"), default="pooled",
                   help="Vakuumnormierung: gemeinsam, zeitlich interpoliert oder mit LO-Monitor")
    p = sub.add_parser("plan", help="Laufplan: Startwerte bzw. Anpassung aus einer Stabilitätsmessung (Allan)")
    p.add_argument("--rate", type=float, default=1000.0, help="Herald-Rate in 1/s")
    p.add_argument("--T-A", dest="T_A", type=float, default=60.0)
    p.add_argument("--T-B", dest="T_B", type=float, default=30.0)
    p.add_argument("--cycles", type=int, default=6)
    p.add_argument("--kv-every", type=int, default=3)
    p.add_argument("--T-KV", dest="T_KV", type=float, default=20.0)
    p.add_argument("--rate-KV", dest="rate_KV", type=float, default=10000.0)
    p.add_argument("--rate-B", dest="rate_B", type=float, default=2000.0)
    p.add_argument("--T-KH", dest="T_KH", type=float, default=60.0)
    p.add_argument("--T-dark", dest="T_dark", type=float, default=10.0, help="Dunkellauf (LO aus) in s, 0 = keiner")
    p.add_argument("--rate-dark", dest="rate_dark", type=float, default=10000.0)
    p.add_argument("--allan", help="CSV (Zeit s, relative Vakuumskala bzw. LO-Leistung) → Allan-Analyse und Anpassungsregel")
    p.add_argument("--target", type=float, default=0.005, help="Ziel σ(η) gesamt")
    p.add_argument("--norm", choices=("pooled", "interpolated", "monitor"), default="interpolated")
    p.add_argument("-o", "--out", help="YAML mit den Blöcken (für hhd-sim run) schreiben")
    a = ap.parse_args(argv)

    try:
        if a.cmd == "params":
            if a.markdown:
                sys.stdout.write(parameter_markdown())
            else:
                for r in parameter_rows():
                    print(f"{r['name']:<40} {str(r['value']):<22} {r['unit']:<8} {r['status']:<10} {r['source']}")
            return 0
        if a.cmd == "config":
            txt = SimConfig().to_yaml()
            if a.out == "-":
                sys.stdout.write(txt)
            else:
                Path(a.out).write_text(txt, encoding="utf-8")
            return 0
        if a.cmd == "run":
            from .engine import run
            cfg = _load(a.config, a.seed, a.scale_duration)
            t = time.time()
            res = run(cfg, a.out)
            print(f"Sitzung {cfg.session.id!r} (Kampagne {cfg.session.campaign!r}, Seed {cfg.session.seed}, "
                  f"Kalibrierung {res.calibration_id}) → {a.out}  [{time.time() - t:.1f} s]")
            for b in res.blocks:
                print(f"  {b.run_id:<9} {b.cfg.id:<12} {b.dataset_type:<15} {b.duration:6.2f} s  Records {len(b.digitizer.timestamp):6d}  "
                      f"T2-Records {len(b.harp_records):8d}" + (f"  W(0,0)soll {b.expected['W00']:+.4f}" if "W00" in b.expected else ""))
                for w in b.warnings:
                    print(f"    Warnung: {w}")
            return 0
        if a.cmd == "campaign":
            from .engine import run_campaign
            cfg = _load(a.config, a.seed, a.scale_duration)
            man = run_campaign(cfg, a.out, a.sessions)
            print(f"Kampagne {man['campaign_id']!r}: {len(man['sessions'])} Sitzungen → {a.out}")
            for s_ in man["sessions"]:
                print(f"  {s_['session_id']:<10} Seed {s_['seed']}  Läufe {', '.join(s_['runs'])}")
            return 0
        if a.cmd == "stream":
            from .engine import simulate
            from .stream import SimStream
            cfg = _load(a.config, a.seed, a.scale_duration)
            res = simulate(cfg)
            dec = {}
            t = time.time()
            for i, c in enumerate(SimStream(res, chunk_s=a.chunk, speed=a.speed, realtime=not a.no_realtime)):
                ev = dec.setdefault(c.block_id, T2Decoder()).decode(c.harp_records)
                print(f"{time.time() - t:7.3f} s  {c.block_id:<10} #{c.index:<4} {c.t_start:6.3f}–{c.t_end:6.3f} s  "
                      f"T2 {len(c.harp_records):6d} (Ereignisse {len(ev.event_time_ps):6d})  Records {len(c.rec_timestamp):5d}  "
                      f"Marker {len(c.marker_timestamp):4d}")
                if a.max_chunks and i + 1 >= a.max_chunks:
                    break
            return 0
        if a.cmd == "plan":
            from .plan import PlanSpec, adapt_plan, make_plan, statistics
            if a.allan:
                from .drift import allan_deviation, fit_noise_model
                d = np.loadtxt(a.allan, delimiter=",", comments="#")
                t, y = d[:, 0], d[:, 1] / np.mean(d[:, 1])
                tau, ad, n = allan_deviation(y, float(np.median(np.diff(t))))
                noise = fit_noise_model(tau, ad, n)
                print("Allan-Abweichung (relativ):")
                for tt, aa in zip(tau, ad):
                    print(f"  τ = {tt:9.1f} s   σ_A = {aa:.2e}")
                print(f"Rauschmodell: weiß {noise['white']:.2e} (τ = 1 s), Irrfahrt q = {noise['q_rw']:.2e}/√s, "
                      f"linear {noise['lin']:.2e}/s")
                r = adapt_plan(noise, R_herald=a.rate, sigma_eta_target=a.target, mode=a.norm)
                spec = PlanSpec(**r["spec"])
                print(f"K-V-Abstand höchstens {r['L_max_s'] / 60:.1f} min; σ(η) gesamt {r['sigma_eta_total']:.4f} "
                      f"(Drift {r['drift_error']['A']:.4f}), {'Ziel erreicht' if r['feasible'] else 'Ziel NICHT erreicht'}")
                print(f"Empfehlung: {r['hint']}")
            else:
                spec = PlanSpec(T_A=a.T_A, T_B=a.T_B, n_cycles=a.cycles, kv_every=a.kv_every, T_KV=a.T_KV,
                                rate_KV=a.rate_KV, rate_B=a.rate_B, T_KH=a.T_KH, T_dark=a.T_dark,
                                rate_dark=a.rate_dark)
            bl = make_plan(spec)
            st = statistics(bl, R_herald=a.rate)
            print(f"Laufplan: {len(bl)} Läufe, {st['t_total_s'] / 60:.1f} min, Daten {st['data_GB']:.1f} GB "
                  f"(max. {st['peak_rate_MBps']:.0f} MB/s)")
            print(f"  Segmente A {st['N']['heralded']:.0f}, B {st['N']['free_running']:.0f}, K-V {st['N']['vacuum']:.0f}, "
                  f"K-H {st['N']['herald_blocked']:.0f}, Dunkel {st['N'].get('electronic', 0):.0f}")
            print(f"  σ(η) {st['sigma_eta']:.4f} (A {st['sigma_eta_stat_A']:.4f}, Vakuum {st['sigma_eta_vac']:.4f}), "
                  f"σ(W00) {st['sigma_W00']:.4f}, K-H: Zusatzvarianz ≥ {100 * st['kh_excess_3sigma_rel']:.2f} % nachweisbar (3σ)")
            print("  " + " → ".join(f"{b['id']}({b['duration_s']:g}s)" for b in bl))
            if a.out:
                import yaml
                head = (f"# Laufplan (hhd-sim plan) für Herald-Rate {a.rate:g}/s – Vorschlag M1, Startwerte vor AP14\n"
                        f"# PlanSpec: {json.dumps(spec.to_dict())}\n"
                        f"# {st['t_total_s'] / 60:.1f} min, {st['data_GB']:.1f} GB, σ(η) {st['sigma_eta']:.4f}, "
                        f"σ(W00) {st['sigma_W00']:.4f}\n")
                Path(a.out).write_text(head + yaml.safe_dump({"source": {"R_herald": a.rate}, "blocks": bl},
                                                             sort_keys=False, allow_unicode=True), encoding="utf-8")
            return 0
        if a.cmd == "validate":
            err = validate_run(a.run_dir)
            print("gültig" if not err else "\n".join(err))
            return 0 if not err else 1
        if a.cmd == "check":
            from .check import check_run
            r = check_run(a.run_dir, n_boot=a.boot, norm_mode=a.norm)
            if a.json:
                print(json.dumps(r, indent=1))
                return 0
            vn = r["vacuum_norm"]
            print(f"Sitzung {r['session_id']} – Vakuumnormierung ({a.norm}): Offset {vn['offset']:.3f} LSB, "
                  f"Skala {vn['scale']:.3f} LSB/√(2·Var), n = {vn['n']}")
            for bid, b in r["blocks"].items():
                if "W00" not in b:
                    print(f"  {bid:<12} {b.get('note', '')}"); continue
                e = b["expected"]
                line = (f"  {bid:<12} n={b['n']:6d}  Var x {b['var_x']:.4f} ± {b['var_err']:.4f}"
                        + (f" (soll {e['var_x']:.4f})" if "var_x" in e else "")
                        + f"  W(0,0) {b['W00']:+.4f} ± {b['W00_err']:.4f}"
                        + (f" (soll {e['W00']:+.4f}, Pull {b.get('W00_pull', 0):+.2f})" if "W00" in e else ""))
                print(line)
                if "phase_fit" in b:
                    pf = b["phase_fit"]
                    print(f"  {'':<12} Phase: A {pf['A']:.4f} ± {pf['A_err']:.4f} (soll {e['A']:.4f}), φ {pf['phi']:+.3f} rad (soll {e['phi']:+.3f})")
            cp = r["comparison"]
            for role, name in (("heralded", "A mit Herald"), ("unheralded", "B ohne Herald")):
                if role in cp:
                    b = cp[role]
                    print(f"Vergleich {name:<14} n={b['n']:6d}  W(0,0) {b['W00']:+.4f} ± {b['W00_err']:.4f}"
                          + (f" (soll {b['expected']['W00']:+.4f})" if "W00" in b["expected"] else "")
                          + f"  Läufe {', '.join(b['runs'])}")
            if "heralded_minus_unheralded" in cp:
                d = cp["heralded_minus_unheralded"]["W00"]
                print(f"  A − B: ΔW(0,0) {d['diff']:+.4f} ± {d['err']:.4f} (z = {d['z']:+.1f})")
            dr = cp["drift"]
            if "aba" in dr:
                print(f"  Drift A-B-A ({dr['aba']['first']} → {dr['aba']['last']}): ΔW(0,0) z = {dr['aba']['W00']['z']:+.1f}, "
                      f"ΔVar z = {dr['aba']['var_x']['z']:+.1f}")
            if "vacuum" in dr:
                print(f"  Drift K-V: Vakuumvarianz {100 * dr['vacuum']['var_rel_change']:+.2f} % (z = {dr['vacuum']['z']:+.1f})")
            if "lo_monitor" in dr:
                print(f"  LO-Monitor: {100 * dr['lo_monitor']['rel_change']:+.2f} %")
            for fl in dr["flags"]:
                print(f"  WARNUNG: {fl}")
            return 0
    except ConfigError as e:
        print(f"Konfigurationsfehler: {e}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
