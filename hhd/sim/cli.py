"""Kommandozeile ``hhd-sim`` (bzw. ``python -m hhd.sim``).

Beispiele::

    hhd-sim params --markdown > docs/PARAMETER.md
    hhd-sim config -o meine.yaml                 # Standardkonfiguration schreiben
    hhd-sim run config/standard.yaml -o out/lauf1
    hhd-sim validate out/lauf1                   # Schema- und Konsistenzprüfung
    hhd-sim check out/lauf1                      # Referenzauswertung gegen Sollwerte (W(0,0), Varianz, Phase)
    hhd-sim stream config/standard.yaml --speed 1 --chunk 0.05
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
        cfg.run.seed = seed
    if scale:
        for b in cfg.blocks:
            b.duration_s *= scale
    return cfg


def validate_run(run_dir: str | Path) -> list[str]:
    d = Path(run_dir)
    err = []
    try:
        man = json.loads((d / "run.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ["run.json fehlt"]
    err += validate_digitizer_file(d / "digitizer.h5")
    import hashlib
    for name, info in man["files"].items():
        p = d / name
        if not p.exists():
            err.append(f"{name} fehlt"); continue
        if hashlib.sha256(p.read_bytes()).hexdigest() != info["sha256"]:
            err.append(f"{name}: Prüfsumme weicht ab")
    for b in man["blocks"]:
        try:
            f = read_ptu(d / b["ptu_file"])
        except Exception as e:      # noqa: BLE001
            err.append(f"{b['ptu_file']}: {e}"); continue
        ev = T2Decoder().decode(f.records)
        if len(ev.event_time_ps) and (ev.event_time_ps.min() < 0 or np.any(np.diff(ev.event_time_ps) < 0)):
            err.append(f"{b['ptu_file']}: Ereigniszeiten nicht monoton")
        if f.tags.get("UsrHHD_BlockId") != b["id"]:
            err.append(f"{b['ptu_file']}: UsrHHD_BlockId passt nicht")
    return err


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hhd-sim", description="Simulator ExSE Harp ADQ32 HHD Mobil (HydraHarp 500 + ADQ32)")
    ap.add_argument("--version", action="version", version=f"hhd {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("params", help="Parametertabelle ausgeben")
    p.add_argument("--markdown", action="store_true")
    p = sub.add_parser("config", help="Standardkonfiguration schreiben")
    p.add_argument("-o", "--out", default="-")
    for name in ("run", "stream"):
        p = sub.add_parser(name, help="Lauf simulieren und Dateien schreiben" if name == "run" else "Datenstrom abspielen")
        p.add_argument("config", nargs="?", help="YAML/JSON (ohne: Standard)")
        p.add_argument("--seed", type=int)
        p.add_argument("--scale-duration", type=float, help="alle Blockdauern multiplizieren")
        if name == "run":
            p.add_argument("-o", "--out", required=True)
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
            print(f"Lauf {cfg.run.name!r} (Seed {cfg.run.seed}) → {a.out}  [{time.time() - t:.1f} s]")
            for b in res.blocks:
                print(f"  {b.cfg.id:<12} {b.dataset_type:<15} {b.duration:6.2f} s  Records {len(b.digitizer.timestamp):6d}  "
                      f"T2-Records {len(b.harp_records):8d}" + (f"  W(0,0)soll {b.expected['W00']:+.4f}" if "W00" in b.expected else ""))
                for w in b.warnings:
                    print(f"    Warnung: {w}")
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
        if a.cmd == "validate":
            err = validate_run(a.run_dir)
            print("gültig" if not err else "\n".join(err))
            return 0 if not err else 1
        if a.cmd == "check":
            from .check import check_run
            r = check_run(a.run_dir, n_boot=a.boot)
            if a.json:
                print(json.dumps(r, indent=1))
                return 0
            vn = r["vacuum_norm"]
            print(f"Vakuumnormierung: Offset {vn['offset']:.3f} LSB, Skala {vn['scale']:.3f} LSB/√(2·Var), n = {vn['n']}")
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
            return 0
    except ConfigError as e:
        print(f"Konfigurationsfehler: {e}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
