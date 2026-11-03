"""Live-Vorschau und Live-Synchronisation während der Messung (Gesamtplan v1.7, AP Server).

Je Chunk der Geräte:

* **Live-Synchronisation** mit ``hhd.sync.stream.StreamingSynchronizer`` (SYNC-Marker Harp ↔ Digitizer;
  Anteil zugeordneter Records/Heralds, Einrasten);
* **Quadratur** je Record mit der nominellen Mode (doppelt exponentiell, γ wie Auswertung) am Ort
  Trigger + Δ_T (nominell, ``delta_T_s``) – eine Vorschau, nicht die Auswertung;
* **Normierung** mit der Vakuumvarianz der bisherigen K-V-Läufe (Var = ½ im Vakuum), Offset aus K-V;
* **LO-Monitor** aus Kanal B (Mittel je Record minus Dunkelwert aus dem Dunkellauf, relativ zum ersten K-V);
* **Raten** (Herald, Records, Marker) und **Warnungen** (LO-Abweichung, Synchronisation, Vakuumvarianz,
  Herald-Rate, fehlende Daten).

Alle Ausgaben sind JSON-fähig (``snapshot()``) und werden vom Server an die Web-App verteilt.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from ..modes.extract import ModeSpec, project_records, template_bank
from ..sync.stream import StreamingSynchronizer
from .devices import DigData, HarpData, RunSpec

KIND_LABEL = dict(vacuum="K-V", electronic="Dunkel", herald_blocked="K-H", heralded="A", free_running="B",
                  random="B", state="Zustand", phase_ref="K-θ")


@dataclass
class LiveParams:
    delta_T_s: float = 76.1e-9          # nominelle Verzögerung Trigger → Modenzentrum
    gamma: float = 1 / 40e-9
    hist_range: float = 4.0             # Histogramm ±x (normiert)
    hist_bins: int = 61
    lo_warn_rel: float = 0.01           # Warnung bei |LO − 1| > 1 %
    vac_warn_z: float = 4.0             # Warnung bei K-V-Varianzänderung > 4σ
    herald_drop_rel: float = 0.2        # Warnung bei Herald-Rate < 80 % des ersten A-Laufs
    trace_len: int = 400
    coupled: bool = True                # False bei gemischtem Betrieb: Geräte nicht gekoppelt → keine Sync-Warnungen


@dataclass
class RunStats:
    spec: RunSpec
    t_wall0: float = field(default_factory=time.time)
    t_run: float = 0.0
    n_records: int = 0
    n_heralds: int = 0
    n_markers_h: int = 0
    n_markers_d: int = 0
    s1: float = 0.0
    s2: float = 0.0
    hist: np.ndarray | None = None
    q_raw: list = field(default_factory=list)       # roh (für die K-V-Normierung)
    lo_sum: float = 0.0
    lo_n: int = 0
    b_sum: float = 0.0
    sync: StreamingSynchronizer | None = None
    done: bool = False
    var: float = float("nan")
    var_err: float = float("nan")
    lo_rel: float = float("nan")
    sync_result: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return dict(block_id=self.spec.block_id, kind=self.spec.kind, label=KIND_LABEL.get(self.spec.kind, self.spec.kind),
                    index=self.spec.index, duration_s=self.spec.duration_s, t_run=round(self.t_run, 3),
                    n_records=self.n_records, n_heralds=self.n_heralds, var=_f(self.var), var_err=_f(self.var_err),
                    lo_rel=_f(self.lo_rel), done=self.done, sync=self.sync_result)


def _f(x):
    x = float(x)
    return None if not np.isfinite(x) else round(x, 6)


class LivePreview:
    def __init__(self, params: LiveParams | None = None, fs: float = 2.5e9, lo_skip: int = 25):
        self.p = params or LiveParams()
        self.fs = fs
        self.lo_skip = lo_skip
        self.mode_shape = "double_exponential"
        self.bank = template_bank(ModeSpec(self.mode_shape, self.p.gamma, fs))
        self.edges = np.linspace(-self.p.hist_range, self.p.hist_range, self.p.hist_bins + 1)
        self.reset()

    def set_mode(self, shape: str, fs: float | None = None) -> None:
        """Modenform (Laserbetrieb: CW double_exponential, Puls gauss) und Abtastrate der Vorschau setzen."""
        fs = float(fs or self.fs)
        if shape != self.mode_shape or fs != self.fs:
            self.mode_shape, self.fs = shape, fs
            self.bank = template_bank(ModeSpec(shape, self.p.gamma, fs))

    def reset(self) -> None:
        self.runs: list[RunStats] = []
        self.cur: RunStats | None = None
        self.offset: float | None = None
        self.vac_var: float | None = None           # rohe K-V-Varianz (LSB²) gepoolt
        self.vac_runs: list[tuple[float, float]] = []
        self.dark_b: float | None = None
        self.lo_ref: float | None = None
        self.herald_rate_ref: float | None = None
        self.trace = dict(t=[], lo=[], rate_h=[], rate_r=[], var=[])
        self.warnings: list[dict] = []
        self._t0 = time.time()
        self._last = None

    # ------------------------------------------------------------------ Lauf
    def begin_run(self, spec: RunSpec) -> None:
        r = RunStats(spec)
        r.hist = np.zeros(self.p.hist_bins, np.int64)
        r.sync = StreamingSynchronizer()
        self.cur = r
        self.runs.append(r)

    def end_run(self) -> RunStats | None:
        r = self.cur
        if r is None:
            return None
        r.done = True
        try:
            res = r.sync.finalize()
            st = r.sync.st
            n_h = max(r.n_heralds, 1)
            r.sync_result = dict(locked=bool(st.locked), n_heralds=r.n_heralds,
                                 matched=int(np.sum(np.asarray(res.get("seg_herald", [])) >= 0))
                                 if isinstance(res, dict) and "seg_herald" in res else None)
            if r.sync_result["matched"] is not None and r.spec.kind == "heralded":
                r.sync_result["matched_frac"] = round(r.sync_result["matched"] / n_h, 4)
        except Exception as e:  # noqa: BLE001
            r.sync_result = dict(locked=False, error=str(e))
        if r.spec.kind == "vacuum" and len(r.q_raw):
            q = np.concatenate(r.q_raw)
            v = float(np.var(q))
            self.vac_runs.append((v, len(q)))
            if len(self.vac_runs) > 1:
                v0, n0 = self.vac_runs[0]
                z = (v - v0) / (v0 * np.sqrt(2 / n0 + 2 / len(q)))
                if abs(z) > self.p.vac_warn_z:
                    self._warn("vakuum", f"Vakuumvarianz {r.spec.block_id} weicht um {z:+.1f}σ vom ersten K-V ab")
            w = np.array([n for _, n in self.vac_runs], float)
            self.vac_var = float(np.sum(w * [v for v, _ in self.vac_runs]) / w.sum())
            if len(self.vac_runs) == 1 and r.lo_n:
                self.lo_ref = r.b_sum / r.lo_n
        if r.spec.kind == "electronic" and r.lo_n:
            dk = r.b_sum / r.lo_n
            if self.lo_ref is not None and abs(self.lo_ref - dk) < 0.05 * max(abs(self.lo_ref), abs(dk), 1.0):
                # LO-Monitor im Dunkellauf praktisch wie mit LO: LO nicht abgeschaltet (oder Kanal B ohne Signal)
                self._warn("lo", f"Dunkellauf {r.spec.block_id}: LO-Monitor unverändert ({dk:.0f} LSB) – LO nicht "
                                 "abgeschaltet? Dunkelwert verworfen")
            else:
                self.dark_b = dk
        if r.spec.kind == "heralded" and r.t_run > 0:
            rate = r.n_heralds / r.t_run
            if self.herald_rate_ref is None:
                self.herald_rate_ref = rate
            elif rate < (1 - self.p.herald_drop_rel) * self.herald_rate_ref:
                self._warn("herald", f"Herald-Rate {rate:.0f}/s < {100 * (1 - self.p.herald_drop_rel):.0f} % "
                                     f"des ersten A-Laufs ({self.herald_rate_ref:.0f}/s)")
        if not self.p.coupled:
            r.sync_result["uncoupled"] = True
        elif r.spec.kind == "heralded" and r.sync_result.get("matched_frac") is not None \
                and r.sync_result["matched_frac"] < 0.9:
            self._warn("sync", f"{r.spec.block_id}: nur {100 * r.sync_result['matched_frac']:.0f} % der Heralds zugeordnet")
        if self.p.coupled and not r.sync_result.get("locked") and r.spec.kind in ("heralded", "herald_blocked"):
            self._warn("sync", f"{r.spec.block_id}: Synchronisation nicht eingerastet")
        if r.n_records == 0:
            self._warn("daten", f"{r.spec.block_id}: keine Records")
        self.cur = None
        return r

    def _warn(self, kind: str, msg: str) -> None:
        self.warnings.append(dict(t=round(time.time() - self._t0, 2), kind=kind, msg=msg))
        self.warnings = self.warnings[-50:]

    # ------------------------------------------------------------------ Daten
    def feed(self, h: HarpData | None, d: DigData | None) -> None:
        r = self.cur
        if r is None:
            return
        if h is not None:
            r.n_heralds += len(h.herald_s)
            r.n_markers_h += len(h.markers_s)
            r.t_run = max(r.t_run, float(h.t_end_s) if np.isfinite(h.t_end_s) else r.t_run)
            r.sync.feed_harp(h.markers_s, h.herald_s)
        if d is not None and len(d.trig_D):
            r.sync.feed_digitizer(d.markers_D, d.trig_D)
            r.n_markers_d += len(d.markers_D)
            data = np.asarray(d.data)
            if self.offset is None:
                self.offset = float(data.mean())
            pos = (d.trig_D + self.p.delta_T_s - d.first_D) * self.fs
            q, _ = project_records(data, pos, self.bank, self.offset)
            r.n_records += len(q)
            r.q_raw.append(q)
            r.s1 += float(q.sum()); r.s2 += float((q * q).sum())
            n = r.n_records
            m = r.s1 / n
            v = max(r.s2 / n - m * m, 0.0)
            if self.vac_var:
                x = q / np.sqrt(2 * self.vac_var)
                r.var = v / (2 * self.vac_var)
                r.var_err = r.var * np.sqrt(2.0 / max(n - 1, 1))
                r.hist += np.histogram(x, self.edges)[0]
            elif r.spec.kind == "vacuum":                 # erster K-V: sich selbst normieren
                r.var = 0.5
                r.var_err = 0.5 * np.sqrt(2.0 / max(n - 1, 1))
                x = q / np.sqrt(2 * v) if v > 0 else q * 0
                r.hist += np.histogram(x, self.edges)[0]
            if d.data_b is not None and len(d.data_b):
                b = float(np.asarray(d.data_b, float).mean())
                r.b_sum += b * len(d.data_b); r.lo_n += len(d.data_b)
                if r.spec.kind == "vacuum" and not self.vac_runs:      # vorläufige Referenz im ersten K-V
                    self.lo_ref = r.b_sum / r.lo_n
                if self.lo_ref is not None and r.spec.kind != "electronic":     # Dunkellauf: LO aus
                    dk = self.dark_b if self.dark_b is not None else 0.0
                    den = self.lo_ref - dk
                    if abs(den) > 1e-9:
                        r.lo_rel = (r.b_sum / r.lo_n - dk) / den
                        lo_now = (b - dk) / den
                        self._push("lo", lo_now)
                        if abs(lo_now - 1) > self.p.lo_warn_rel and \
                                (self._last is None or time.time() - self._last > 5):
                            self._last = time.time()
                            self._warn("lo", f"LO-Leistung {100 * (lo_now - 1):+.1f} % (Lauf {r.spec.block_id})")
        dt = max(r.t_run, 1e-9)
        self._push("rate_h", r.n_heralds / dt, t=True)
        self._push("rate_r", r.n_records / dt)
        self._push("var", r.var if np.isfinite(r.var) else None)

    def _push(self, key: str, v, t: bool = False) -> None:
        tr = self.trace
        if t:
            tr["t"].append(round(time.time() - self._t0, 2))
        tr[key].append(None if v is None or not np.isfinite(v) else round(float(v), 6))
        for k in tr:
            if len(tr[k]) > self.p.trace_len:
                del tr[k][: len(tr[k]) - self.p.trace_len]

    # ------------------------------------------------------------------ Ausgabe
    def snapshot(self) -> dict:
        r = self.cur or (self.runs[-1] if self.runs else None)
        cur = None
        if r is not None:
            dt = max(r.t_run, 1e-9)
            cur = r.summary()
            cur.update(rate_herald=round(r.n_heralds / dt, 1), rate_records=round(r.n_records / dt, 1),
                       hist=r.hist.tolist() if r.hist is not None else [], locked=bool(r.sync.st.locked) if r.sync else False,
                       progress=round(min(r.t_run / r.spec.duration_s, 1.0), 3) if r.spec.duration_s else None)
        return dict(current=cur, runs=[x.summary() for x in self.runs], edges=[round(e, 4) for e in self.edges],
                    trace=self.trace, warnings=self.warnings[-20:], normalized=self.vac_var is not None,
                    vac_var_lsb2=_f(self.vac_var) if self.vac_var else None)
