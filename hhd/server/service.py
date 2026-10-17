"""Messdienst des HHD-Servers: Geräte, Messablauf (Sitzung nach Laufplan), Live-Vorschau und Auswertungsaufträge.

Der Messablauf läuft in einem eigenen Thread (ein Thread ruft die Geräte auf, wie in GX); die Offline-Auswertung
(``hhd.analysis.ensure_sync`` + ``analyze_session``) läuft als Auftrag in einem eigenen Prozess, damit Vorschau
und Server während der Rechnung bedienbar bleiben. Die Befehle der Web-App kommen über ``handle``.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import multiprocessing as mp
import threading
import time
import tomllib
import traceback
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .. import __version__
from .control import (ALLOWED_CMDS, CommandError, CommandLog, ControlLock, ParamPolicy, safe_session_dir)
from .devices import DeviceError, RunSpec, make_devices
from .live import KIND_LABEL, LiveParams, LivePreview

PLANS = {"standard": "M1 Zeitraffer (18 Läufe)", "kurz": "Kurz (7 Läufe)"}
PLAN_DOC = {"standard": "K-V, Dunkel, K-H, 6×(A, B) + A mit K-V nach jedem 3. Zyklus und am Ende",
            "kurz": "K-V, Dunkel, K-H, A, B, A, K-V"}
STATES = {"bereit": "bereit", "vorbereitung": "Simulation/Vorbereitung", "messung": "Messung läuft",
          "schreiben": "Dateien schreiben", "fertig": "Sitzung abgeschlossen", "abgebrochen": "abgebrochen",
          "fehler": "Fehler"}


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"                 # LAN; für den Tunnel genügt 127.0.0.1
    port: int = 8766
    token: str = ""                       # leer = ohne Zugangscode (nur LAN/lokal!)
    data_dir: str = "hhd_data"
    log_dir: str = "logs"
    device_mode: str = "sim"              # live | sim | replay
    sim_config: str = "config/standard.yaml"
    replay_dir: str = ""
    chunk_s: float = 0.1
    speed: float = 1.0                    # Echtzeitfaktor (Simulation/Wiedergabe)
    realtime: bool = True
    time_scale: float = 0.0               # 0 = Laufdauern wie Konfiguration
    lock_timeout_s: float = 300.0
    auto_analyze: bool = True
    n_boot: int = 100
    status_hz: float = 2.0
    delta_T_ns: float = 76.1
    seed0: int = 20261100
    analysis_inprocess: bool = False      # Tests: Auswertung im selben Prozess


def load_config(path: str | Path | None) -> ServerConfig:
    cfg = ServerConfig()
    if path and Path(path).exists():
        d = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        flat = dict(d.get("server", {})) | dict(d.get("devices", {})) | dict(d.get("analysis", {}))
        names = {f.name for f in fields(ServerConfig)}
        unknown = set(flat) - names
        if unknown:
            raise ValueError(f"{path}: unbekannte Einträge {sorted(unknown)}")
        for k, v in flat.items():
            setattr(cfg, k, type(getattr(cfg, k))(v))
    return cfg


def run_analysis_job(session_dir: str, n_boot: int) -> dict:
    """Auftrag: Synchronisation (falls nötig) und Auswertung; Rückgabe Kurzfassung."""
    from ..analysis.pipeline import AnalysisParams, analyze_session
    from ..analysis.syncdata import ensure_sync
    from ..io.session import load_session
    t0 = time.time()
    ensure_sync(load_session(session_dir))
    t_sync = time.time() - t0
    res = analyze_session(session_dir, AnalysisParams(n_boot=n_boot))
    return dict(t_sync=round(t_sync, 2), t_total=round(time.time() - t0, 2), warnings=res.get("warnings", [])[:20])


@dataclass
class Job:
    id: str
    session: str
    state: str = "wartet"                 # wartet | läuft | fertig | fehler
    t_submit: float = field(default_factory=time.time)
    t_start: float | None = None
    t_end: float | None = None
    error: str = ""
    info: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["dauer_s"] = round((self.t_end or time.time()) - (self.t_start or time.time()), 1) if self.t_start else None
        return d


class MeasurementService:
    def __init__(self, cfg: ServerConfig, root: Path | None = None):
        self.cfg = cfg
        self.root = Path(root) if root else Path.cwd()
        self.data_dir = self._abs(cfg.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lock = ControlLock(cfg.lock_timeout_s)
        self.log = CommandLog(self._abs(cfg.log_dir))
        self.policy = ParamPolicy()
        self.backend, self.harp, self.dig = make_devices(
            cfg.device_mode, base_config=self._abs(cfg.sim_config) if cfg.sim_config else None,
            replay_dir=self._abs(cfg.replay_dir) if cfg.replay_dir else None, chunk_s=cfg.chunk_s, speed=cfg.speed,
            realtime=cfg.realtime, time_scale=cfg.time_scale or None)
        self.device_error = ""
        for d in (self.harp, self.dig):
            try:
                d.open()
            except DeviceError as e:
                self.device_error = (self.device_error + " " + str(e)).strip()
        self.live = LivePreview(LiveParams(delta_T_s=cfg.delta_T_ns * 1e-9))
        self.display = dict(hist_range=4.0, plan="standard", speed=cfg.speed, auto_analyze=cfg.auto_analyze,
                            n_boot=cfg.n_boot)
        self.state = "bereit"
        self.session_id = ""
        self.plan: list[RunSpec] = []
        self.run_idx = -1
        self.msg = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._mtx = threading.RLock()
        self.jobs: dict[str, Job] = {}
        self._pool = None if cfg.analysis_inprocess else cf.ProcessPoolExecutor(1, mp_context=mp.get_context("spawn"))
        self._job_thread_pool = cf.ThreadPoolExecutor(1)
        self.n_sessions = 0
        self.listeners: list = []             # Rückrufe bei Zustandswechsel (Server: sofort senden)

    def _abs(self, p: str | Path) -> Path:
        p = Path(p)
        return p if p.is_absolute() else (self.root / p)

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)
        self._job_thread_pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ Status
    @property
    def simulated(self) -> bool:
        return self.cfg.device_mode != "live"

    def status(self) -> dict:
        with self._mtx:
            live = self.live.snapshot()
        return dict(type="status", version=__version__, mode=self.cfg.device_mode, simulated=self.simulated,
                    state=self.state, state_text=STATES.get(self.state, self.state), msg=self.msg,
                    session_id=self.session_id, run_index=self.run_idx,
                    plan=[dict(r.to_dict(), label=KIND_LABEL.get(r.kind, r.kind)) for r in self.plan],
                    plans=PLANS, plan_doc=PLAN_DOC, lock=self.lock.state(), display=self.display, live=live,
                    jobs=[j.to_dict() for j in sorted(self.jobs.values(), key=lambda j: -j.t_submit)][:10],
                    devices=dict(harp=self.harp.info(), digitizer=self.dig.info()), device_error=self.device_error,
                    server_time=time.strftime("%Y-%m-%d %H:%M:%S"))

    def _notify(self) -> None:
        for f in list(self.listeners):
            try:
                f()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ Befehle
    def handle(self, client: str, addr: str, local: bool, cmd: str, params: dict | None, name: str = "") -> dict:
        params = params or {}
        try:
            if cmd not in ALLOWED_CMDS:
                raise CommandError(f"unbekannter Befehl {cmd!r}")
            if not isinstance(params, dict):
                raise CommandError("'params' muss ein Objekt sein")
            if cmd == "acquire_control":
                if not self.lock.acquire(client, name or addr):
                    raise CommandError(f"Steuerung belegt durch {self.lock.holder_name or 'anderen Client'}")
                res = dict(ok=True, msg="Steuerung übernommen")
            elif cmd == "release_control":
                self.lock.release(client)
                res = dict(ok=True, msg="Steuerung abgegeben")
            else:
                if not self.lock.check(client):
                    raise CommandError("keine Steuerberechtigung – zuerst Steuerung übernehmen")
                res = getattr(self, "_cmd_" + cmd)(params, client=client, local=local)
        except (CommandError, DeviceError, ValueError) as e:
            res = dict(ok=False, msg=str(e))
        except Exception as e:  # noqa: BLE001
            res = dict(ok=False, msg=f"interner Fehler: {e}")
        logp = {k: v for k, v in params.items() if k != "confirm"} | ({"confirm": "***"} if "confirm" in params else {})
        self.log.write(client, addr, cmd, logp, bool(res.get("ok")), res.get("msg", ""), local)
        self._notify()
        return res

    def _cmd_start_session(self, p: dict, **_) -> dict:
        if self._thread and self._thread.is_alive():
            raise CommandError("eine Sitzung läuft bereits")
        if self.device_error:
            raise CommandError(f"Geräte nicht bereit: {self.device_error}")
        plan = p.get("plan", self.display["plan"])
        if plan not in PLANS:
            raise CommandError(f"Laufplan {plan!r} unbekannt ({', '.join(PLANS)})")
        speed = float(p.get("speed", self.display["speed"]))
        if not 0.05 <= speed <= 50:
            raise CommandError("speed: 0,05 … 50")
        note = str(p.get("note", ""))[:200]
        self.display.update(plan=plan, speed=speed)
        if self.backend is not None:
            self.backend.speed = speed
        self._stop.clear()
        sid = time.strftime("HHD-%Y%m%d-%H%M%S")
        if (self.data_dir / sid).exists():
            sid += f"-{uuid.uuid4().hex[:4]}"
        self.session_id = sid
        self._thread = threading.Thread(target=self._run_session, args=(sid, plan, note), daemon=True)
        self._thread.start()
        return dict(ok=True, msg=f"Sitzung {sid} gestartet ({plan})", session_id=sid)

    def _cmd_stop_session(self, p: dict, **_) -> dict:
        if not (self._thread and self._thread.is_alive()):
            raise CommandError("keine laufende Sitzung")
        self._stop.set()
        return dict(ok=True, msg="Stopp angefordert (aktueller Lauf wird beendet, Dateien werden geschrieben)")

    def _cmd_set_display(self, p: dict, **_) -> dict:
        allowed = {"hist_range": (1.0, 10.0), "speed": (0.05, 50.0), "n_boot": (0, 1000)}
        out = {}
        for k, v in p.items():
            if k == "plan":
                if v not in PLANS:
                    raise CommandError(f"Laufplan {v!r} unbekannt")
                out[k] = v
            elif k == "auto_analyze":
                out[k] = bool(v)
            elif k in allowed:
                x = float(v)
                lo, hi = allowed[k]
                if not lo <= x <= hi:
                    raise CommandError(f"{k}: {lo} … {hi}")
                out[k] = int(x) if k == "n_boot" else x
            else:
                raise CommandError(f"unbekannte Anzeigeeinstellung {k!r}")
        self.display.update(out)
        if "speed" in out and self.backend is not None:
            self.backend.speed = out["speed"]
        return dict(ok=True, msg="Einstellungen übernommen", applied=out)

    def _cmd_set_device(self, p: dict, client: str, local: bool, **_) -> dict:
        dev = {"harp": self.harp, "digitizer": self.dig}.get(p.get("device"))
        if dev is None:
            raise CommandError("device: harp | digitizer")
        params = p.get("params")
        verdict, info = self.policy.check(dev, params, local, client, p.get("confirm"))
        if verdict == "confirm":
            return dict(ok=False, needs_confirm=True, confirm=info,
                        msg="Kritischer Geräteparameter: Änderung aus der Ferne bitte ausdrücklich bestätigen (ED-8b)")
        applied = dev.configure(**params)
        return dict(ok=True, msg=f"{dev.kind}: {', '.join(f'{k}={v}' for k, v in applied.items())} ({info})",
                    applied=applied)

    def _cmd_analyze(self, p: dict, **_) -> dict:
        sid = p.get("session") or self.session_id
        safe_session_dir(self.data_dir, sid)
        j = self.submit_analysis(sid)
        return dict(ok=True, msg=f"Auswertung {sid} beauftragt", job=j.id)

    def _cmd_clear_warnings(self, p: dict, **_) -> dict:
        with self._mtx:
            self.live.warnings.clear()
        return dict(ok=True, msg="Warnungen gelöscht")

    # ------------------------------------------------------------------ Messablauf
    def _run_session(self, sid: str, plan: str, note: str) -> None:
        out = self.data_dir / sid
        n_done = 0
        try:
            self.state, self.msg = "vorbereitung", "Laufplan wird vorbereitet"
            self._notify()
            with self._mtx:
                self.live.reset()
            if self.cfg.device_mode == "sim":
                self.n_sessions += 1
                cfg = self.backend.make_config(sid, self.cfg.seed0 + self.n_sessions, plan)
                runs = self.backend.prepare(cfg)
            elif self.cfg.device_mode == "replay":
                runs = self.backend.prepare()
            else:
                raise DeviceError("Betriebsart live: Geräte-SDK nicht eingebunden")
            fs = float(self.backend.device_attrs.get("sampling_rate_Hz", 2.5e9))
            with self._mtx:
                self.live.fs = fs
            self.plan = runs
            self.state, self.msg = "messung", ""
            for i, run in enumerate(runs):
                if self._stop.is_set():
                    break
                self.run_idx = i
                self.msg = f"Lauf {i + 1}/{len(runs)}: {run.block_id} ({KIND_LABEL.get(run.kind, run.kind)})"
                with self._mtx:
                    self.live.begin_run(run)
                self.backend.start_run(i)
                for d in (self.harp, self.dig):
                    d.arm(run); d.start()
                self._notify()
                h_done = d_done = False
                while not (h_done and d_done):
                    h = None if h_done else self.harp.read()
                    d = None if d_done else self.dig.read()
                    if h is None:
                        h_done = True
                    elif h.run_done:
                        h_done = True
                    if d is None:
                        d_done = True
                    elif d.run_done:
                        d_done = True
                    with self._mtx:
                        self.live.feed(h, d)
                    if self._stop.is_set():
                        break
                for d in (self.harp, self.dig):
                    d.stop()
                if self._stop.is_set() and not (h_done and d_done):
                    with self._mtx:
                        self.live.cur = None
                        self.live.runs.pop()
                    break
                with self._mtx:
                    self.live.end_run()
                n_done = i + 1
                self._notify()
            self.state, self.msg = "schreiben", f"{n_done} Läufe werden geschrieben"
            self._notify()
            if n_done:
                self.backend.write_files(out, n_done)
                meta = dict(format="hhd-server-session", software=f"hhd {__version__}", mode=self.cfg.device_mode,
                            simulated=self.simulated, plan=plan, note=note, runs_done=n_done, runs_planned=len(runs),
                            stopped=self._stop.is_set(), devices=dict(harp=self.harp.info()["params"],
                                                                      digitizer=self.dig.info()["params"]))
                (out / "server_session.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
                with self._mtx:
                    snap = self.live.snapshot()
                (out / "live_preview.json").write_text(json.dumps(dict(runs=snap["runs"], warnings=snap["warnings"]),
                                                                  indent=1, ensure_ascii=False), encoding="utf-8")
            stopped = self._stop.is_set()
            self.state = "abgebrochen" if stopped else "fertig"
            self.msg = f"Sitzung {sid}: {n_done}/{len(runs)} Läufe" + (" (gestoppt)" if stopped else "")
            self.run_idx = -1
            if n_done and self.display.get("auto_analyze") and not stopped:
                self.submit_analysis(sid)
        except Exception as e:  # noqa: BLE001
            self.state, self.msg = "fehler", f"{type(e).__name__}: {e}"
            traceback.print_exc()
        finally:
            for d in (self.harp, self.dig):
                d.stop()
            self._notify()

    # ------------------------------------------------------------------ Aufträge
    def submit_analysis(self, sid: str) -> Job:
        j = Job(uuid.uuid4().hex[:8], sid)
        self.jobs[j.id] = j
        self._job_thread_pool.submit(self._run_job, j)
        return j

    def _run_job(self, j: Job) -> None:
        j.state, j.t_start = "läuft", time.time()
        self._notify()
        try:
            path = str(self.data_dir / j.session)
            nb = int(self.display.get("n_boot", self.cfg.n_boot))
            if self._pool is None:
                j.info = run_analysis_job(path, nb)
            else:
                j.info = self._pool.submit(run_analysis_job, path, nb).result()
            j.state = "fertig"
        except Exception as e:  # noqa: BLE001
            j.state, j.error = "fehler", f"{type(e).__name__}: {e}"
        j.t_end = time.time()
        self._notify()

    def wait_idle(self, timeout: float = 600.0) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            busy = (self._thread and self._thread.is_alive()) or any(j.state in ("wartet", "läuft") for j in self.jobs.values())
            if not busy:
                return True
            time.sleep(0.1)
        return False

    # ------------------------------------------------------------------ Sitzungen/Ergebnisse
    def sessions(self) -> list[dict]:
        out = []
        for d in sorted(self.data_dir.iterdir(), reverse=True):
            if not (d / "session.json").exists():
                continue
            meta = {}
            if (d / "server_session.json").exists():
                meta = json.loads((d / "server_session.json").read_text(encoding="utf-8"))
            res = d / "analysis" / "result.json"
            out.append(dict(id=d.name, plan=meta.get("plan"), runs=meta.get("runs_done"), simulated=meta.get("simulated", True),
                            note=meta.get("note", ""), analyzed=res.exists(),
                            mtime=time.strftime("%Y-%m-%d %H:%M", time.localtime(d.stat().st_mtime)),
                            job=next((j.state for j in self.jobs.values() if j.session == d.name), None)))
        return out

    def result(self, sid: str) -> dict:
        p = safe_session_dir(self.data_dir, sid) / "analysis" / "result.json"
        if not p.exists():
            raise CommandError("noch keine Auswertung")
        return json.loads(p.read_text(encoding="utf-8"))

    def summary(self, sid: str) -> dict:
        return result_summary(self.result(sid))

    def report(self, sid: str) -> str:
        p = safe_session_dir(self.data_dir, sid) / "analysis" / "report.md"
        if not p.exists():
            raise CommandError("noch kein Bericht")
        return p.read_text(encoding="utf-8")

    def live_record(self, sid: str) -> dict:
        p = safe_session_dir(self.data_dir, sid) / "live_preview.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


GROUP_KEYS = ("n", "var_x", "var_err", "W00", "W00_ci68", "W00_ci95", "W00_err", "eta_fit", "eta_fit_err",
              "eta_fit_ci68", "eta_fit_ci95", "eta_var", "mean_n", "g2", "mandel_Q", "fidelity_fock1",
              "fidelity_lossy_fock1", "p_n", "wigner_r", "wigner_radial", "estimator", "W00_ci_method",
              "eta_fit_ci_method", "W00_moments", "W00_moments_err")


def result_summary(r: dict) -> dict:
    """Kompakte Ergebnisansicht für die App (Wigner, Photonenstatistik, Vergleich A/B, Budget, Warnungen)."""
    rec = r.get("reconstruction", {})
    groups = {g: {k: rec[g].get(k) for k in GROUP_KEYS if k in rec[g]} for g in rec if isinstance(rec[g], dict)}
    comp = r.get("comparison", {}) or {}
    keep = {k: comp.get(k) for k in ("histogram", "ks", "wigner_r", "wigner_diff", "diff_var_x", "diff_W00",
                                     "diff_eta_fit", "eta_from_comparison", "null_tests") if k in comp}
    cal = r.get("calibration", {}) or {}
    return dict(session_id=r.get("session_id"), software=r.get("software"), schema=r.get("schema"),
                groups=groups, comparison=keep, efficiency_budget=r.get("efficiency_budget"),
                calibration={k: cal.get(k) for k in ("var_kv", "eta_el", "eta_el_err", "clearance", "normalization",
                                                    "n_kv", "n_el") if k in cal},
                drift=r.get("drift_vacuum_lo"), warnings=r.get("warnings", []), performance=r.get("performance"))
