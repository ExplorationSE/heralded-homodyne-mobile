"""Speichersparender Datenstrom-Simulator: Läufe in Zeitscheiben (``slice_s``) erzeugen und sofort schreiben.

``simulate`` (``hhd.sim.engine``) hält eine ganze Sitzung im Speicher (alle Records, Wahrheit, Uhr- und
Driftgitter) – gut für Prüfungen, aber der Speicher wächst mit der Laufdauer. Für lange Läufe (Driftstudien über
Stunden) erzeugt :class:`StreamingSimulator` jeden Lauf scheibenweise mit ``simulate_block`` und schreibt die
Scheibe sofort (``hhd.io.recorder``: PTU-Datei im Datenstrom, ``digitizer.h5`` mit erweiterbaren Datensätzen).
Der Speicherbedarf hängt nur von ``slice_s`` (und den Raten) ab, nicht von der Lauf- oder Sitzungsdauer
(Nachweis: ``tests/test_streaming_sim.py``).

Unterschiede zum Datei-Modus (bewusst, dokumentiert):

* Zufallszahlen je Scheibe (Seed-Folge je Lauf und Scheibe) – Statistik gleich, Dateien nicht bitgleich;
* Moden/Records, die eine Scheibengrenze überlappen, fehlen (Anteil ≈ Recordlänge / ``slice_s``, z. B. 2 µs/1 s);
* Laufmarken (M1) und Phasenreferenz-Marker werden nicht erzeugt (pro Scheibe sinnlos);
* Uhr-Irrfahrt und stochastische Drift werden **lazy** in Segmenten erzeugt (:class:`LazyWalk`); die Wahrheit
  (``truth.h5``) enthält nur eine **dezimierte** Driftkurve und je Lauf die Sollwerte zur Laufmitte.

Voraussetzungen: ``slice_s · markers.f_marker`` und ``slice_s · periodic_rate`` ganzzahlig (Marker- und
Periodenraster laufen über Scheibengrenzen weiter), ``phase.mode`` ≠ ``ramp``/``steps``.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path

import h5py
import numpy as np

from .. import __version__
from ..io import modes as M
from ..io import ptu as P
from ..io import schema as S
from ..io.recorder import SessionRecorder
from .config import SimConfig
from .digitizer import ADQ32
from .engine import eta_of_run, lo_scale_fn, plan_block_attrs, simulate_block
from .harp import HydraHarp500


class LazyWalk:
    """Irrfahrt w(t) auf einem Gitter ``dt`` (w(t_s) = 0), segmentweise erzeugt; nur die letzten Segmente bleiben
    im Speicher. Abfragen müssen (bis auf ``keep`` Segmente) zeitlich vorwärts laufen."""

    def __init__(self, sigma_step: float, dt: float, t_start: float, rng: np.random.Generator, seg: int = 4096,
                 keep: int = 2):
        self.s, self.dt, self.t0, self.rng, self.seg, self.keep = sigma_step, dt, t_start, rng, seg, keep
        self.segs: dict[int, np.ndarray] = {}
        self._next = 0
        self._last = 0.0

    def _ensure(self, k: int) -> None:
        while self._next <= k:
            st = self.rng.normal(0.0, self.s, self.seg)
            w = self._last + np.cumsum(st)
            if self._next == 0:
                w = np.concatenate([[0.0], w[:-1]])
            self.segs[self._next] = w
            self._last = float(w[-1])
            self._next += 1
            for old in [i for i in self.segs if i < self._next - 1 - self.keep]:
                del self.segs[old]

    def __call__(self, t) -> np.ndarray:
        t = np.asarray(t, float)
        if t.size == 0:
            return np.zeros(t.shape)
        x = (t - self.t0) / self.dt
        i_hi = int(np.floor(np.max(x))) + 1
        k_lo, k_hi = int(np.floor(max(np.min(x), 0) / self.seg)), i_hi // self.seg
        self._ensure(k_hi)
        if k_lo not in self.segs:
            raise RuntimeError("LazyWalk: Abfrage vor dem gehaltenen Zeitfenster (Zeit läuft rückwärts)")
        ks = range(k_lo, k_hi + 1)
        grid = np.concatenate([self.segs[k] for k in ks])
        tg = self.t0 + self.dt * (k_lo * self.seg + np.arange(len(grid)))
        return np.interp(t, tg, grid)


class StreamClock:
    """Wie ``DigitizerClock``, aber ohne Gitter über die ganze Sitzung (Irrfahrt lazy)."""

    def __init__(self, cfg, rng: np.random.Generator):
        self.eps = 0.0 if cfg.shared_10MHz else cfg.eps
        self.D = 0.0 if cfg.shared_10MHz else cfg.rw_D
        self.t_arm = cfg.t_arm_s
        self.dt = cfg.grid_dt
        self._w = LazyWalk(np.sqrt(self.D * self.dt), self.dt, 0.0, rng) if self.D > 0 else None

    def phi(self, t):
        return self._w(t) if self._w is not None else np.zeros(np.shape(t))

    def __call__(self, t):
        t = np.asarray(t, dtype=float)
        return (1 + self.eps) * (t - self.t_arm) + self.phi(t)

    def inverse(self, TD):
        TD = np.asarray(TD, dtype=float)
        t = TD / (1 + self.eps) + self.t_arm
        if self._w is None:
            return t
        for _ in range(3):
            t = (TD - self.phi(t)) / (1 + self.eps) + self.t_arm
        return t


class StreamDriftPath:
    """Wie ``DriftPath`` (gleiches Modell), Irrfahrten lazy; ``truth()`` liefert eine dezimierte Kurve."""

    def __init__(self, drift_cfg, lo_monitor_cfg, t_start: float, rng: np.random.Generator, max_truth: int = 2048):
        from .drift import _Component
        d = drift_cfg
        self.t_s = float(t_start)
        self.comp = dict(
            lo=_Component(d.lo_rel_per_s, d.lo_rw_per_sqrt_s, d.lo_sin_amp, d.lo_sin_period_s),
            eta=_Component(d.eta_rel_per_s, d.eta_rw_per_sqrt_s, d.eta_sin_amp, d.eta_sin_period_s),
            mon=_Component(0.0, getattr(lo_monitor_cfg, "gain_rw_per_sqrt_s", 0.0), 0.0, 0.0))
        self.stochastic = any(c.rw > 0 or c.sin_amp != 0 for c in self.comp.values())
        dt = float(d.grid_dt_s)
        self.walk, self.phases = {}, {}
        for name in ("lo", "eta", "mon"):
            c = self.comp[name]
            sub = np.random.default_rng(rng.integers(1 << 62))
            self.walk[name] = LazyWalk(c.rw * np.sqrt(dt), dt, t_start, sub) if c.rw > 0 else None
            self.phases[name] = float(rng.uniform(0, 2 * np.pi)) if c.sin_amp else 0.0
        self._truth = TruthDecimator(max_truth)

    def factor(self, name: str, t) -> np.ndarray:
        c = self.comp[name]
        t = np.asarray(t, float)
        x = 1.0 + c.lin * (t - self.t_s)
        if self.walk[name] is not None:
            x = x * (1.0 + self.walk[name](t))
        if c.sin_amp and c.sin_period > 0:
            x = x * (1.0 + c.sin_amp * np.sin(2 * np.pi * (t - self.t_s) / c.sin_period + self.phases[name]))
        return x

    def lo(self, t):
        return self.factor("lo", t)

    def eta(self, t):
        return self.factor("eta", t)

    def monitor_gain(self, t):
        return self.factor("mon", t)

    def sample(self, t: float) -> None:
        self._truth.add(t, float(self.lo(t)), float(self.eta(t)), float(self.monitor_gain(t)))

    def truth(self) -> dict:
        a = self._truth.array()
        return dict(t_grid=a[:, 0], lo=a[:, 1], eta_factor=a[:, 2], monitor_gain=a[:, 3])


class TruthDecimator:
    """Höchstens ``n_max`` Stützstellen: bei Überlauf jede zweite verwerfen und den Abstand verdoppeln."""

    def __init__(self, n_max: int = 2048):
        self.n_max, self.rows, self.stride, self._k = n_max, [], 1, 0

    def add(self, *row) -> None:
        if self._k % self.stride == 0:
            self.rows.append(row)
            if len(self.rows) > self.n_max:
                self.rows = self.rows[::2]
                self.stride *= 2
        self._k += 1

    def array(self) -> np.ndarray:
        return np.array(self.rows, float).reshape(-1, 4) if self.rows else np.empty((0, 4))


@dataclass
class _H:
    records: np.ndarray


@dataclass
class _D:
    data: np.ndarray
    raw: dict


@dataclass
class _Run:
    block_id: str
    kind: str
    duration_s: float
    index: int
    run_id: str
    extra: dict


class StreamingSimulator:
    """Sitzung scheibenweise simulieren und schreiben. ``on_slice(info, rec, data_b, t_session)`` (optional)
    erhält je Scheibe die Records (z. B. für ``hhd.analysis.driftmon.DriftMonitor``)."""

    def __init__(self, cfg: SimConfig, slice_s: float = 1.0, on_slice=None):
        self.cfg = copy.deepcopy(cfg)
        self.cfg.harp.run_marker_bit = 0                       # keine Laufmarken je Scheibe
        self.cfg.phase.marker_bit = 0
        self.slice_s = float(slice_s)
        self.on_slice = on_slice
        mk = self.cfg.markers.f_marker * self.slice_s
        if abs(mk - round(mk)) > 1e-9:
            raise ValueError(f"slice_s · f_marker = {mk} muss ganzzahlig sein")
        for b in self.cfg.blocks:
            pr = b.periodic_rate * self.slice_s
            if b.kind in ("vacuum", "electronic", "free_running") and abs(pr - round(pr)) > 1e-9:
                raise ValueError(f"{b.id}: slice_s · periodic_rate = {pr} muss ganzzahlig sein")
            if self.cfg.phase_for(b).mode in ("ramp", "steps"):
                raise ValueError(f"{b.id}: Phasenmodus {self.cfg.phase_for(b).mode} im Datenstrom-Simulator nicht unterstützt")
        self.n_records = 0

    def run(self, out_dir: str | Path) -> dict:
        cfg = self.cfg
        warnings = cfg.validate()
        se = cfg.session
        root = np.random.SeedSequence(se.seed)
        ss_clock, ss_blocks = root.spawn(2)
        clk = StreamClock(cfg.clock, np.random.default_rng(ss_clock))
        path = StreamDriftPath(cfg.drift, cfg.lo_monitor, se.start_s, np.random.default_rng(root.spawn(1)[0]))
        path_used = path if path.stochastic else None
        adq, harp = ADQ32(cfg, clk), HydraHarp500(cfg)
        dev = adq.device_attrs()
        cal = cfg.calibration_id
        meta = dict(session_id=se.id, campaign_id=se.campaign, calibration_id=cal)
        rec = SessionRecorder(Path(out_dir), M.SIM, meta, dev,
                              extra_manifest=dict(seed=se.seed, config=cfg.to_dict(),
                                                  streaming=dict(slice_s=self.slice_s, generator="hhd.sim.streaming")))
        runs_truth = []
        t0 = se.start_s
        for i, (b, ssb, rid) in enumerate(zip(cfg.blocks, ss_blocks.spawn(len(cfg.blocks)), cfg.run_ids())):
            attrs = plan_block_attrs(cfg, b, i, t0, cal, rid)
            run = _Run(b.id, b.kind, float(b.duration_s), i, rid, dict(attrs=attrs))
            rec.begin_run(run)
            n_sl = max(1, int(round(b.duration_s / self.slice_s)))
            enc = _T2Shift()
            for j, ss in enumerate(ssb.spawn(n_sl)):
                dur = min(self.slice_s, b.duration_s - j * self.slice_s)
                if dur <= 0:
                    break
                bj = copy.copy(b)
                bj.duration_s = dur
                ts = t0 + j * self.slice_s
                br = simulate_block(cfg, bj, i, ts, clk, adq, harp, ss, cal, rid, drift_path=path_used)
                dg = br.digitizer
                rec.feed(_H(enc.shift(br.harp_records, int(round(j * self.slice_s * 1e12)))),
                         _D(dg.data, dict(timestamp=dg.timestamp, record_start=dg.record_start,
                                          trigger_source=dg.trigger_source, markers=dg.marker_ts, data_b=dg.data_b,
                                          marker_b=None)))
                self.n_records += len(dg.timestamp)
                if path_used is not None:
                    path.sample(ts + dur / 2)
                if self.on_slice is not None:
                    self.on_slice(dict(block_id=b.id, kind=b.kind, index=i, slice=j, t_session=ts - se.start_s),
                                  dg, ts - se.start_s)
                del br, dg
            rec.end_run(True)
            t_mid = t0 + b.duration_s / 2
            runs_truth.append(dict(id=b.id, run_id=rid, kind=b.kind, t_mid_s=t_mid - se.start_s,
                                   lo_scale=float(lo_scale_fn(cfg, b, path_used)(t_mid)),
                                   eta_state=eta_of_run(cfg, b, t_mid, path_used)))
            t0 += b.duration_s + se.gap_s
        man = rec.close()
        out = Path(out_dir)
        with h5py.File(out / "truth.h5", "w") as f:              # dezimierte Wahrheit (keine Ereignisse)
            f.attrs.update(format=S.FORMAT_TRUTH, schema_version=S.SCHEMA_VERSION, software=f"hhd {__version__}",
                           config_json=cfg.to_json(), streaming=True, warnings_json=json.dumps(warnings))
            if path_used is not None:
                g = f.create_group("drift")
                for k, v in path.truth().items():
                    g.create_dataset(k, data=v)
            for r in runs_truth:
                g = f.create_group(f"blocks/{r['id']}")
                g.attrs.update(run_id=r["run_id"], kind=r["kind"], t_mid_s=r["t_mid_s"], lo_scale=r["lo_scale"],
                               eta_state=r["eta_state"])
        from .engine import _sha256
        man["files"]["truth.h5"] = dict(role="truth", sha256=_sha256(out / "truth.h5"))
        man["truth"] = runs_truth
        (out / "session.json").write_text(json.dumps(man, indent=1, ensure_ascii=False, default=float), encoding="utf-8")
        return man


class _T2Shift:
    """T2-Records einer Scheibe (Zeit ab Scheibenbeginn) an den Laufstrom anhängen: Zeiten um den Scheibenbeginn
    verschieben und die Überläufe relativ zum zuletzt geschriebenen Ereignis neu kodieren."""

    def __init__(self):
        self.base = 0                                    # Wrap-Periode des letzten geschriebenen Ereignisses

    def shift(self, rec: np.ndarray, offset_ps: int) -> np.ndarray:
        ev = P.T2Decoder().decode(rec)
        t = np.concatenate([ev.event_time_ps, ev.marker_time_ps]) + offset_ps
        kinds = np.concatenate([np.where(ev.event_channel == 0, P.KIND_SYNC, P.KIND_EVENT),
                                np.full(len(ev.marker_time_ps), P.KIND_MARKER)])
        codes = np.concatenate([np.where(ev.event_channel == 0, 0, ev.event_channel), ev.marker_bits])
        if not len(t):
            return np.empty(0, np.uint32)
        codes = np.where(kinds == P.KIND_SYNC, 1, codes)       # Code beim Sync ohne Bedeutung
        rel = np.maximum(t - self.base * P.T2_WRAP, 0)        # Hardware liefert zeitlich geordnet
        out, _ = P.encode_t2(rel, kinds, codes)
        self.base += int(np.max(rel)) // P.T2_WRAP
        return out
