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
* Optional (``record_truth_dir``, ab 0.13) die Zuordnungswahrheit je Record in ``truth_records.h5``
  (:class:`RecordTruthWriter`, blockweise angehängt: Triggerart, Harp-Zeit des auslösenden Heralds, Dunkelereignis),
  damit Fehlzuordnungen auch bei Läufen über Stunden geprüft werden können (``hhd validate-records``).

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


class _ExpectedMean:
    """Sollwerte eines Laufs (``expected`` von ``simulate_block``) als mit der Recordzahl gewichtetes Mittel über
    die Scheiben (Drift im Lauf); nicht-skalare Einträge von der letzten Scheibe. Für ``hhd validate-analysis``."""

    def __init__(self):
        self.s: dict = {}; self.w = 0.0; self.last: dict = {}

    def add(self, exp: dict, n: int) -> None:
        w = float(max(n, 1))
        for k, v in exp.items():
            if isinstance(v, (int, float, np.floating, np.integer)) and not isinstance(v, bool):
                self.s[k] = self.s.get(k, 0.0) + w * float(v)
        self.w += w
        self.last = exp

    def result(self) -> dict:
        out = {k: v for k, v in self.last.items() if k not in self.s}
        out.update({k: v / self.w for k, v in self.s.items()})
        out["averaged_over_slices"] = True
        return out


class RecordTruthWriter:
    """``truth_records.h5``: je Lauf ``blocks/<id>/{kind, herald_ps, is_dark}`` (eine Zeile je Digitizer-Record,
    Reihenfolge wie in ``digitizer.h5``), erweiterbar, scheibenweise angehängt. ``herald_ps`` = Harp-Zeit (ps ab
    Laufbeginn) des auslösenden Heralds, −1 ohne Herald bzw. ohne Harp-Ereignis. Attribut ``complete`` erst nach
    der letzten Scheibe."""

    FILE = "truth_records.h5"

    def __init__(self, out_dir: str | Path):
        self.path = Path(out_dir) / self.FILE
        self.f = h5py.File(self.path, "a")
        self.f.attrs.update(format="hhd-truth-records", software=f"hhd {__version__}")

    def begin(self, block_id: str, run_id: str) -> None:
        if f"blocks/{block_id}" in self.f:
            del self.f[f"blocks/{block_id}"]
        g = self.f.create_group(f"blocks/{block_id}")
        g.attrs.update(run_id=run_id, complete=False)
        for k, dt in (("kind", np.int8), ("herald_ps", np.int64), ("is_dark", np.bool_)):
            g.create_dataset(k, (0,), dtype=dt, maxshape=(None,), chunks=(1 << 16,))
        self.g = g

    def add(self, truth: dict, offset_ps: int) -> None:
        rec, he = truth["records"], truth["heralds"]
        kind = np.asarray(rec["kind"]).astype(np.int8)
        src = np.asarray(rec["source_index"], np.int64)
        from .digitizer import TRIG_HERALD
        hp = np.asarray(he["harp_ps"], np.int64)
        dark = np.asarray(he["is_dark"], bool)
        isH = (kind == TRIG_HERALD) & (src >= 0) & (src < len(hp))
        herald_ps = np.full(len(kind), -1, np.int64)
        is_dark = np.zeros(len(kind), bool)
        if isH.any():
            ps = hp[src[isH]]
            herald_ps[isH] = np.where(ps >= 0, ps + offset_ps, -1)
            is_dark[isH] = dark[src[isH]]
        n0 = self.g["kind"].shape[0]
        for k, v in (("kind", kind), ("herald_ps", herald_ps), ("is_dark", is_dark)):
            self.g[k].resize((n0 + len(v),))
            self.g[k][n0:] = v

    def end(self) -> None:
        self.g.attrs["complete"] = True
        self.f.flush()

    def close(self) -> None:
        if self.f:
            self.f.close()
            self.f = None


class StreamingSimulator:
    """Sitzung scheibenweise simulieren und schreiben. ``on_slice(info, rec, data_b, t_session)`` (optional)
    erhält je Scheibe die Records (z. B. für ``hhd.analysis.driftmon.DriftMonitor``)."""

    def __init__(self, cfg: SimConfig, slice_s: float = 1.0, on_slice=None, record_truth_dir: str | Path | None = None):
        self.cfg = copy.deepcopy(cfg)
        self.record_truth_dir = record_truth_dir
        self._rtw: RecordTruthWriter | None = None
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

    # ------------------------------------------------------------------ schrittweise Schnittstelle
    def setup(self) -> list[_Run]:
        """Sitzung vorbereiten (Uhr, Drift, Geräte, Seeds); liefert die Läufe. Danach ``iter_run(i)`` für
        i = 0, 1, … in dieser Reihenfolge (Uhr- und Drift-Irrfahrt laufen nur vorwärts)."""
        cfg = self.cfg
        self.warnings = cfg.validate()
        se = cfg.session
        root = np.random.SeedSequence(se.seed)
        ss_clock, ss_blocks = root.spawn(2)
        self._clk = StreamClock(cfg.clock, np.random.default_rng(ss_clock))
        self._path = StreamDriftPath(cfg.drift, cfg.lo_monitor, se.start_s, np.random.default_rng(root.spawn(1)[0]))
        self._path_used = self._path if self._path.stochastic else None
        self._adq, self._harp = ADQ32(cfg, self._clk), HydraHarp500(cfg)
        self.device_attrs = self._adq.device_attrs()
        self.calibration_id = cfg.calibration_id
        self.meta = dict(session_id=se.id, campaign_id=se.campaign, calibration_id=self.calibration_id)
        self.extra_manifest = dict(seed=se.seed, config=cfg.to_dict(),
                                   streaming=dict(slice_s=self.slice_s, generator="hhd.sim.streaming"))
        self.runs, self._ss, self._t0 = [], [], []
        t0 = se.start_s
        for i, (b, ssb, rid) in enumerate(zip(cfg.blocks, ss_blocks.spawn(len(cfg.blocks)), cfg.run_ids())):
            attrs = plan_block_attrs(cfg, b, i, t0, self.calibration_id, rid)
            self.runs.append(_Run(b.id, b.kind, float(b.duration_s), i, rid, dict(attrs=attrs)))
            self._ss.append(ssb)
            self._t0.append(t0)
            t0 += b.duration_s + se.gap_s
        self.runs_truth = []
        self._next_run = 0
        return self.runs

    def iter_run(self, i: int):
        """Scheiben des Laufs ``i``: je Scheibe ``(info, harp_records, dg)`` mit ``harp_records`` als T2-Strom des
        Laufs (Zeit ab Laufbeginn, Überläufe fortlaufend) und ``dg`` den Digitizer-Records
        (``hhd.sim.digitizer.DigitizerBlock``). Die Wahrheit des Laufs wird erst nach der letzten Scheibe
        festgehalten (mit der letzten Scheibe; abgebrochene Läufe zählen nicht)."""
        if i != self._next_run:
            raise RuntimeError(f"Läufe nur der Reihe nach (erwartet {self._next_run}, nicht {i})")
        self._next_run += 1
        cfg, se = self.cfg, self.cfg.session
        b, ssb, t0 = cfg.blocks[i], self._ss[i], self._t0[i]
        rid = self.runs[i].run_id
        n_sl = max(1, int(round(b.duration_s / self.slice_s)))
        enc = _T2Shift()
        exp_acc = _ExpectedMean()
        if self.record_truth_dir is not None:
            if self._rtw is None:
                self._rtw = RecordTruthWriter(self.record_truth_dir)
            self._rtw.begin(b.id, rid)
        for j, ss in enumerate(ssb.spawn(n_sl)):
            dur = min(self.slice_s, b.duration_s - j * self.slice_s)
            if dur <= 0:
                break
            bj = copy.copy(b)
            bj.duration_s = dur
            ts = t0 + j * self.slice_s
            br = simulate_block(cfg, bj, i, ts, self._clk, self._adq, self._harp, ss, self.calibration_id, rid,
                                drift_path=self._path_used)
            dg = br.digitizer
            hrec = enc.shift(br.harp_records, int(round(j * self.slice_s * 1e12)))
            if self._rtw is not None:
                self._rtw.add(br.truth, int(round(j * self.slice_s * 1e12)))
            self.n_records += len(dg.timestamp)
            exp_acc.add(br.expected, len(dg.timestamp))
            if self._path_used is not None:
                self._path.sample(ts + dur / 2)
            last = j == n_sl - 1 or (j + 1) * self.slice_s >= b.duration_s
            info = dict(block_id=b.id, kind=b.kind, index=i, slice=j, t_session=ts - se.start_s,
                        t_run_end=j * self.slice_s + dur, last=last)
            if last:                                   # Wahrheit vor der letzten Scheibe (Leser hören dort auf)
                if self._rtw is not None:
                    self._rtw.end()
                t_mid = t0 + b.duration_s / 2
                self.runs_truth.append(dict(id=b.id, run_id=rid, kind=b.kind, t_mid_s=t_mid - se.start_s,
                                            lo_scale=float(lo_scale_fn(cfg, b, self._path_used)(t_mid)),
                                            eta_state=eta_of_run(cfg, b, t_mid, self._path_used),
                                            expected=exp_acc.result()))
            yield info, hrec, dg
            del br, dg
            if last:
                break

    def write_truth(self, out_dir: str | Path, man: dict | None = None, run_ids: set | None = None) -> dict:
        """``truth.h5`` (dezimiert, keine Ereignisse) schreiben und ``session.json`` ergänzen."""
        cfg = self.cfg
        out = Path(out_dir)
        if man is None:
            man = json.loads((out / "session.json").read_text(encoding="utf-8"))
        rt = [r for r in self.runs_truth if run_ids is None or r["id"] in run_ids]
        if self._rtw is not None:
            self._rtw.close()
            self._rtw = None
        with h5py.File(out / "truth.h5", "w") as f:
            f.attrs.update(format=S.FORMAT_TRUTH, schema_version=S.SCHEMA_VERSION, software=f"hhd {__version__}",
                           config_json=cfg.to_json(), streaming=True, warnings_json=json.dumps(self.warnings))
            if self._path_used is not None:
                g = f.create_group("drift")
                for k, v in self._path.truth().items():
                    g.create_dataset(k, data=v)
            for r in rt:
                g = f.create_group(f"blocks/{r['id']}")
                g.attrs.update(run_id=r["run_id"], kind=r["kind"], t_mid_s=r["t_mid_s"], lo_scale=r["lo_scale"],
                               eta_state=r["eta_state"])
                if r.get("expected") is not None:
                    g.attrs["expected_json"] = json.dumps(r["expected"], default=float)
        from .engine import _sha256
        man.setdefault("files", {})["truth.h5"] = dict(role="truth", sha256=_sha256(out / "truth.h5"))
        if (out / RecordTruthWriter.FILE).exists():
            man["files"][RecordTruthWriter.FILE] = dict(role="truth_records", sha256=_sha256(out / RecordTruthWriter.FILE))
        man["truth"] = [{k: v for k, v in r.items() if k != "expected"} for r in rt]
        for k, v in self.extra_manifest.items():
            man.setdefault(k, v)
        (out / "session.json").write_text(json.dumps(man, indent=1, ensure_ascii=False, default=float), encoding="utf-8")
        return man

    def run(self, out_dir: str | Path) -> dict:
        runs = self.setup()
        rec = SessionRecorder(Path(out_dir), M.SIM, self.meta, self.device_attrs, extra_manifest=self.extra_manifest)
        for i, run in enumerate(runs):
            rec.begin_run(run)
            for info, hrec, dg in self.iter_run(i):
                rec.feed(_H(hrec), _D(dg.data, dict(timestamp=dg.timestamp, record_start=dg.record_start,
                                                    trigger_source=dg.trigger_source, markers=dg.marker_ts,
                                                    data_b=dg.data_b, marker_b=None,
                                                    aux=dict(ramp_raw=dg.ramp_raw) if dg.ramp_raw is not None else None)))
                if self.on_slice is not None:
                    self.on_slice({k: info[k] for k in ("block_id", "kind", "index", "slice", "t_session")}, dg,
                                  info["t_session"])
            rec.end_run(True)
        man = rec.close()
        return self.write_truth(out_dir, man)


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
