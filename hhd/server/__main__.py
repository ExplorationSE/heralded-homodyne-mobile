"""Start des HHD-Mess-Servers: ``python -m hhd.server [--config server.toml] [--host …] [--port …]``."""
from __future__ import annotations

import argparse
from pathlib import Path

from .service import load_config


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hhd.server", description="HHD-Mess-Server (Web-App, Geräte, Auswertung)")
    ap.add_argument("--config", default="server.toml", help="Konfigurationsdatei (TOML), Vorlage config/server.toml")
    ap.add_argument("--host"); ap.add_argument("--port", type=int)
    ap.add_argument("--mode", choices=["live", "sim", "replay"], help="Betriebsart beider Geräte")
    ap.add_argument("--harp-mode", choices=["live", "sim", "replay"], help="nur HydraHarp 500 (gemischter Betrieb)")
    ap.add_argument("--digitizer-mode", choices=["live", "sim", "replay"], help="nur ADQ32 (gemischter Betrieb)")
    ap.add_argument("--replay-dir")
    ap.add_argument("--data-dir"); ap.add_argument("--log-dir"); ap.add_argument("--token")
    ap.add_argument("--speed", type=float)
    ap.add_argument("--sim-engine", choices=["batch", "stream"],
                    help="Simulator: batch (Sitzung im Speicher) oder stream (Zeitscheiben, beschränkter Speicher)")
    a = ap.parse_args(argv)
    cfg = load_config(a.config if Path(a.config).exists() else None)
    for k, v in dict(host=a.host, port=a.port, device_mode=a.mode, harp_mode=a.harp_mode,
                     digitizer_mode=a.digitizer_mode, replay_dir=a.replay_dir, data_dir=a.data_dir, log_dir=a.log_dir,
                     token=a.token, speed=a.speed, sim_engine=a.sim_engine).items():
        if v is not None:
            setattr(cfg, k, v)
    import uvicorn
    from .app import create_app
    app = create_app(cfg, Path.cwd())
    hm, dm = cfg.harp_mode or cfg.device_mode, cfg.digitizer_mode or cfg.device_mode
    eng = f"  Simulator: {cfg.sim_engine}" if "sim" in (hm, dm) else ""
    print(f"HHD-Mess-Server: http://{cfg.host}:{cfg.port}/  HydraHarp: {hm.upper()}  ADQ32: {dm.upper()}{eng}"
          + ("  (Zugangscode aktiv)" if cfg.token else "  (ohne Zugangscode – nur LAN/lokal!)"))
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning", ws_ping_interval=20)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
