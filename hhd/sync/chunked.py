"""Synchronisation sehr langer Einzelläufe in Zeitsegmenten (beschränkter Speicher).

Der Batch-Lauf (``hhd.sync.pipeline``) hält je Lauf alle Marker, Heralds und Digitizer-Köpfe im Speicher; bei
Läufen von Stunden wächst das mit der Laufdauer. :class:`ChunkedRunSync` liest PTU und ``digitizer.h5`` in
Zeitfenstern (``hhd.io.chunked``) und arbeitet in Segmenten von ``segment_s`` (Digitizer-Zeit):

1. **Durchgang 1** je Segment (Daten des Segments ± ``overlap_s``):
   - Marker paaren (``match_markers``) und die Uhr anpassen (``fit_clock``, robuste Gerade plus stückweise
     Korrektur mit Knotenabstand aus Kreuzvalidierung);
   - Heralds zuordnen, die Abbildung mit Herald/Trigger-Paaren verfeinern (``refine_clock``, inklusive
     stückweiser Korrektur), erneut zuordnen;
   - Kenngrößen des Laufs sammeln (δ_HT, σ_HT) und saubere Records für Δ per Reservoir-Stichprobe ziehen.
2. **Δ** (sitzungsweit, ``hhd.sync.pipeline.calibrate_delay``) aus der Stichprobe; höchstens
   ``delay_max_records`` Records, also konstanter Speicher.
3. **Durchgang 2** je Segment mit der zusammengesetzten Abbildung :class:`SegmentedClock` (je Segment eine
   ``ClockMap``, an den Grenzen über ``overlap_s`` linear überblendet, stetig): endgültige Zuordnung mit
   Modenort Δ, Schreiben in ``sync.h5`` blockweise (erweiterbare Datensätze).

Ein Record gehört dem Segment, in dem sein Trigger liegt; ein Herald dem Segment, in dem sein erwarteter
Trigger (abgebildeter Herald + δ_HT des Laufs) liegt. Gerechnet wird jeweils mit einem Rand (Kontext), damit
Nachbarn (Totzeit, Fremdmoden, Überlappung) an Segmentgrenzen gleich behandelt werden wie im Batch-Lauf.
Ausgabe und Bericht haben dasselbe Format wie der Batch-Lauf (``clock.model = "segmented"``).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import h5py
import numpy as np

from ..io import chunked as CK
from ..io import ptu as P
from ..io.session import RunData, split_events
from . import assign as A
from . import kernels as K
from .clockmap import ClockMap, fit_clock
from .markers import match_markers

ROLES = ("herald", "sync_marker", "adq32_trigger_copy", "random_tag", "state_tag")


@dataclass
class ChunkParams:
    segment_s: float = 30.0        # Segmentlänge (Digitizer-Zeit)
    overlap_s: float = 1.0         # Rand je Seite für Uhrfit/Verfeinerung; Überblendung über overlap_s
    read_s: float = 1.0            # Lesefenster der Dateien
    context_s: float = 50e-6       # Rand der endgültigen Zuordnung (≫ Record + Totzeit)
    herald_margin_s: float = 20e-6  # Auswahl der Heralds um das abgebildete Fenster (|δ_HT| + Δ)
    coarse_s: float = 2.0          # Dauer für die grobe Startabbildung
    coarse_margin_s: float = 0.2   # Unsicherheit der groben Startabbildung (Harp-Zeit)


# ---------------------------------------------------------------------------------------------- Abbildung
class SegmentedClock:
    """Uhrabbildung aus Segmenten: ``maps[k]`` gilt um [bounds[k], bounds[k+1]) (Digitizer-Zeit); an jeder inneren
    Grenze b wird über [b − ov/2, b + ov/2] linear von maps[k] auf maps[k+1] überblendet (stetig)."""

    def __init__(self, bounds: list[float], maps: list[ClockMap], overlap_s: float):
        self.bounds = np.asarray(bounds, float)          # innere Grenzen, len(maps) − 1
        self.maps = maps
        self.ov = float(overlap_s)

    @property
    def b(self) -> float:
        return float(np.mean([m.b for m in self.maps]))

    def H(self, T) -> np.ndarray:
        T = np.asarray(T, float)
        if len(self.maps) == 1:
            return self.maps[0].H(T)
        k = np.searchsorted(self.bounds, T, side="right")
        out = np.empty(T.shape)
        for i in np.unique(k):
            m = k == i
            out[m] = self.maps[i].H(T[m])
        h = self.ov / 2
        for j, bnd in enumerate(self.bounds):
            m = np.abs(T - bnd) < h
            if m.any():
                w = (T[m] - (bnd - h)) / self.ov
                out[m] = (1 - w) * self.maps[j].H(T[m]) + w * self.maps[j + 1].H(T[m])
        return out

    def D(self, tH) -> np.ndarray:
        tH = np.asarray(tH, float)
        if len(self.maps) == 1:
            return self.maps[0].D(tH)
        # Startwert: Segment über die Harp-Zeit der Grenzen, dann Newton mit der Steigung
        hb = np.array([self.maps[j].H(np.array([bnd]))[0] for j, bnd in enumerate(self.bounds)])
        k = np.searchsorted(hb, tH, side="right")
        T = np.empty(tH.shape)
        for i in np.unique(k):
            m = k == i
            T[m] = self.maps[i].D(tH[m])
        for _ in range(4):
            T = T - (self.H(T) - tH) / self.b
        return T

    def to_dict(self) -> dict:
        m0 = self.maps[0]
        sr = float(np.sqrt(np.mean([m.sigma_r ** 2 for m in self.maps])))
        return dict(m0.to_dict(), sigma_r=sr, n_used=int(sum(m.n_used for m in self.maps)), pieces=[],
                    segmented=dict(bounds=self.bounds.tolist(), overlap_s=self.ov,
                                   maps=[m.to_dict() for m in self.maps]))

    @classmethod
    def from_dict(cls, d: dict) -> "SegmentedClock":
        s = d["segmented"]
        return cls(s["bounds"], [ClockMap.from_dict(m) for m in s["maps"]], s["overlap_s"])


# ---------------------------------------------------------------------------------------------- Leser
class _HarpReader:
    """Harp-Ereignisse nach Rolle in einem gleitenden Puffer (Harp-Zeit, s); globale Indizes je Rolle."""

    def __init__(self, run: RunData, read_s: float):
        self.gen = CK.iter_ptu_windows(run.ptu_path, 0, int(round(read_s * 1e12)))
        self.dec = P.T2Decoder()
        self.roles = (run.channel_roles, run.marker_roles)
        self.read_s = read_s
        self.buf = {r: np.empty(0) for r in ROLES}
        self.dropped = {r: 0 for r in ROLES}
        self.covered = -np.inf
        self.done = False
        self.n_events = 0

    def until(self, t: float) -> None:
        while not self.done and self.covered < t:
            try:
                k, raw = next(self.gen)
            except StopIteration:
                self.done, self.covered = True, np.inf
                break
            ev = self.dec.decode(raw)
            self.n_events += len(ev.event_time_ps) + len(ev.marker_time_ps)
            hs = split_events(ev, *self.roles)
            for r in ROLES:
                x = hs.role_s(r)
                if len(x):
                    self.buf[r] = np.concatenate([self.buf[r], np.sort(x)])
            self.covered = (k + 1) * self.read_s

    def get(self, role: str, lo: float, hi: float) -> tuple[np.ndarray, int]:
        """Ereignisse in [lo, hi) und globaler Index des ersten."""
        b = self.buf[role]
        i0, i1 = np.searchsorted(b, lo, "left"), np.searchsorted(b, hi, "left")
        return b[i0:i1], self.dropped[role] + int(i0)

    def drop_before(self, t: float) -> None:
        for r in ROLES:
            i = int(np.searchsorted(self.buf[r], t, "left"))
            if i:
                self.dropped[r] += i
                self.buf[r] = self.buf[r][i:]


class _DigReader:
    """Digitizer-Köpfe (Trigger, erstes Sample, globaler Index) und SYNC-Marker in einem gleitenden Puffer."""

    def __init__(self, run: RunData, read_s: float):
        tu = run.time_unit
        with h5py.File(run.session.h5_path, "r") as f:
            g = f["blocks"][run.block_id]
            ts, mk = g["records/timestamp"], g["markers/timestamp"]
            c = [int(ts[0])] if ts.shape[0] else []
            c += [int(np.min(mk[: min(mk.shape[0], 1 << 16)]))] if mk.shape[0] else []
            self.n_total_r, self.n_total_m = int(ts.shape[0]), int(mk.shape[0])
        self.n_r = self.n_m = 0
        self.t0_tu = min(c) if c else 0
        self.tu = tu
        w = max(1, int(round(read_s / tu)))
        self.rg = CK.iter_record_windows(run.session.h5_path, run.block_id, self.t0_tu, w)
        self.mg = CK.iter_marker_windows(run.session.h5_path, run.block_id, self.t0_tu, w)
        self.w_s = w * tu
        self.idx = np.empty(0, np.int64); self.trig = np.empty(0); self.first = np.empty(0)
        self.mk = np.empty(0)
        self.cov_r = self.cov_m = -np.inf
        self.done_r = self.done_m = False
        self.t_max = -np.inf

    @property
    def t0(self) -> float:
        return self.t0_tu * self.tu

    @property
    def done(self) -> bool:
        """Alle Records und Marker der Datei gelesen."""
        return (self.done_r or self.n_r >= self.n_total_r) and (self.done_m or self.n_m >= self.n_total_m)

    def until(self, T: float) -> None:
        while not self.done_r and self.cov_r < T:
            try:
                k, w = next(self.rg)
            except StopIteration:
                self.done_r, self.cov_r = True, np.inf
                break
            self.n_r += len(w.index)
            if len(w.index):
                tr = w.timestamp.astype(np.float64) * self.tu
                self.idx = np.concatenate([self.idx, w.index])
                self.trig = np.concatenate([self.trig, tr])
                self.first = np.concatenate([self.first, (w.timestamp + w.record_start).astype(np.float64) * self.tu])
                self.t_max = max(self.t_max, float(tr[-1]))
            self.cov_r = self.t0 + (k + 1) * self.w_s
        while not self.done_m and self.cov_m < T:
            try:
                k, m, _ = next(self.mg)
            except StopIteration:
                self.done_m, self.cov_m = True, np.inf
                break
            self.n_m += len(m)
            if len(m):
                x = m.astype(np.float64) * self.tu
                self.mk = np.concatenate([self.mk, x])
                self.t_max = max(self.t_max, float(x[-1]))
            self.cov_m = self.t0 + (k + 1) * self.w_s

    def records(self, lo: float, hi: float):
        i0, i1 = np.searchsorted(self.trig, lo, "left"), np.searchsorted(self.trig, hi, "left")
        return self.idx[i0:i1], self.trig[i0:i1], self.first[i0:i1]

    def markers(self, lo: float, hi: float) -> np.ndarray:
        return self.mk[np.searchsorted(self.mk, lo, "left"):np.searchsorted(self.mk, hi, "left")]

    def drop_before(self, T: float) -> None:
        i = int(np.searchsorted(self.trig, T, "left"))
        if i:
            self.idx, self.trig, self.first = self.idx[i:], self.trig[i:], self.first[i:]
        j = int(np.searchsorted(self.mk, T, "left"))
        if j:
            self.mk = self.mk[j:]


# ---------------------------------------------------------------------------------------------- Lauf
class _Acc:
    """Laufende Summen für Kenngrößen über Segmente."""

    def __init__(self):
        self.d: dict[str, float] = {}

    def add(self, **kw) -> None:
        for k, v in kw.items():
            self.d[k] = self.d.get(k, 0.0) + float(v)

    def get(self, k: str, default: float = 0.0) -> float:
        return self.d.get(k, default)


@dataclass
class _Seg:
    lo: float                    # Kern (Digitizer-Zeit); −inf/+inf am Laufrand
    hi: float
    fit_lo: float
    fit_hi: float
    cmap: ClockMap
    fit: dict
    marker: dict
    refine: dict = field(default_factory=dict)
    dHT: float = float("nan")    # δ_HT des Segments (nach Verfeinerung) und Zahl der Paare
    n_m: int = 0


class ChunkedRunSync:
    """Synchronisation eines Laufs in Segmenten; Schnittstelle wie ``RunSync`` für ``sync_session``."""

    def __init__(self, run: RunData, p, cp: ChunkParams | None = None):
        self.run, self.p, self.cp = run, p, cp or ChunkParams()
        self.segs: list[_Seg] = []
        self.cmap: SegmentedClock | None = None
        self.delta: dict = {}
        self.warnings: list[str] = []
        self.assign_warnings: list[str] = []
        self.timing: dict = {}
        self.n_events = 0
        self.dHT = float("nan"); self.sigma_HT = float("nan"); self.n_matched_p1 = 0
        self.delta_candidates: list[tuple[np.ndarray, np.ndarray]] = []   # (Index, pos0) je Segment, Reservoir
        self.final: dict = {}
        self.assignment = None                       # kein Batch-Objekt; Δ nutzt die Reservoir-Stichprobe
        self.chunked = True

    @property
    def key(self) -> str:
        return self.run.block_id

    # -------------------------------------------------------------------- gemeinsame Größen
    def _kw(self, tags, tag_cls):
        run, p = self.run, self.p
        pre = -float(run.device.get("horizontal_offset", 0)) / run.fs
        post = run.record_length / run.fs - pre
        return dict(rearm=p.rearm_s + 1.0 / run.fs, pretrig_s=pre, post_s=post, tol_min=p.tol_min_s,
                    k_sigma=p.k_sigma, tags=tags, tag_class=tag_cls)

    def _last(self, rec_first):
        return rec_first + (self.run.record_length - 1) / self.run.fs

    def _tags(self, hr: _HarpReader, lo_h: float, hi_h: float):
        for role, cls in (("random_tag", A.SEG_RANDOM), ("state_tag", A.SEG_STATE)):
            if len(hr.buf[role]) or hr.dropped[role]:
                t, _ = hr.get(role, lo_h, hi_h)
                return t, cls
        return None, A.SEG_RANDOM

    # -------------------------------------------------------------------- Durchgang 1
    def pass1(self, reservoir) -> None:
        from .pipeline import _herald_triggered, refine_clock
        t0 = time.perf_counter()
        run, p, cp = self.run, self.p, self.cp
        hr, dr = _HarpReader(run, cp.read_s), _DigReader(run, cp.read_s)
        herald_run = _herald_triggered(run)
        S, ov = cp.segment_s, cp.overlap_s
        T0 = dr.t0
        # grobe Startabbildung aus den ersten Sekunden
        dr.until(T0 + cp.coarse_s + cp.read_s)
        hr.until(cp.coarse_s + cp.coarse_margin_s + 1.0)
        mm0 = match_markers(hr.buf["sync_marker"], dr.markers(-np.inf, T0 + cp.coarse_s), p.marker_period_s)
        approx, _ = fit_clock(mm0.TD, mm0.tH, allow_piecewise=False, floor=1e-12)
        margin = cp.coarse_margin_s
        acc = _Acc()
        k = 0
        warn_set: list[str] = []
        u_max = 0.0
        while True:
            lo, hi = T0 + k * S, T0 + (k + 1) * S
            dr.until(hi + 0.5 * S + ov)
            last = dr.done and dr.t_max < hi + 0.5 * S
            core_lo = -np.inf if k == 0 else lo
            core_hi = np.inf if last else hi
            f_lo, f_hi = lo - ov, (dr.t_max + 1e-9 if last else hi + ov)
            TDm = dr.markers(f_lo, f_hi)
            h_lo, h_hi = approx.H(np.array([f_lo, f_hi])) + np.array([-margin, margin])
            hr.until(h_hi)
            tHm, _ = hr.get("sync_marker", h_lo, h_hi)
            mm = match_markers(tHm, TDm, p.marker_period_s)
            for w in mm.warnings:
                if w not in warn_set:
                    warn_set.append(w)
            u_max = max(u_max, mm.uniqueness)
            cm, info = fit_clock(mm.TD, mm.tH, allow_piecewise=p.allow_piecewise, floor=1e-12)
            res, used = info["residuals"], info["used"]
            core = (mm.TD >= core_lo) & (mm.TD < core_hi)
            acc.add(n_pairs=core.sum(), res2=np.sum(res[used & core] ** 2), n_res=np.sum(used & core),
                    n_out=np.sum(~used & core), piecewise=float(info["model"] == "piecewise"))
            acc.d["res_max"] = max(acc.get("res_max"), float(np.max(np.abs(res[used & core]))) if (used & core).any() else 0.0)
            seg = _Seg(core_lo, core_hi, f_lo, f_hi, cm, dict(model=info["model"], W=info.get("W"),
                                                                 sigma_r=info["sigma_r"]),
                       dict(n_pairs=int(mm.n_pairs), uniqueness=float(mm.uniqueness), shift=int(mm.shift)))
            # Zuordnung und Verfeinerung (nur Herald-getriggerte Läufe, wie im Batch-Lauf)
            if herald_run:
                idx, trig, first = dr.records(f_lo, f_hi)
                hh = cm.H(np.array([f_lo, f_hi])) + np.array([-cp.herald_margin_s, cp.herald_margin_s])
                hr.until(hh[1])
                tH, _ = hr.get("herald", *hh)
                tags, tcls = self._tags(hr, *hh)
                run_D = tuple(cm.D(np.array([0.0, run.duration_s])))
                kw = self._kw(tags, tcls)
                if len(trig):
                    a = A.assign_heralds(tH, cm, trig, first, self._last(first), run_D, **kw)
                    if p.refine:
                        seg.refine = refine_clock(cm, a, tH, trig, p)
                        if seg.refine.get("applied"):
                            a = A.assign_heralds(tH, cm, trig, first, self._last(first), run_D, **kw)
                    own = (trig >= core_lo) & (trig < core_hi)
                    nm = int(np.sum(own & (a.seg_class == A.SEG_MATCHED)))
                    acc.add(dHT_w=a.dHT * nm, sHT2_w=a.sigma_HT ** 2 * nm, n_m=nm)
                    seg.dHT, seg.n_m = float(a.dHT), nm
                    ok = own & (a.seg_class == A.SEG_MATCHED) & (a.seg_n_secondary == 0) & ~a.seg_overlap_prev
                    if run.dataset_type == "heralded" and ok.any():
                        reservoir.add(self.key, idx[ok], (trig[ok] - first[ok]) * run.fs)
            self.segs.append(seg)
            approx, margin = cm, 2e-3
            # Puffer: alles vor dem nächsten Fit-Fenster (minus Kontext) verwerfen
            nlo = hi - ov - cp.read_s
            dr.drop_before(nlo)
            hr.drop_before(float(cm.H(np.array([nlo]))[0]) - cp.coarse_margin_s)
            k += 1
            if last:
                break
        hr.until(np.inf)
        self.n_events = int(hr.n_events + run.n_records)
        nm = acc.get("n_m")
        self.dHT = acc.get("dHT_w") / nm if nm else float("nan")
        # gemeinsamer Bezug: der Versatz jeder Segmentabbildung kommt aus ihren Markern (bei Markern am Marker-
        # Eingang ns-genau), die Herald/Trigger-Paare sind viel genauer. Jedes Segment wird so verschoben, dass
        # sein δ_HT gleich dem gepoolten δ_HT des Laufs ist – dann gilt Modenort = D(t_H) + Δ_T + δ_HT überall,
        # und die Abbildung ist an den Segmentgrenzen stetig im Sinne der Paare.
        self.offset_shifts = []
        if np.isfinite(self.dHT):
            for sg in self.segs:
                if sg.n_m > 0 and np.isfinite(sg.dHT):
                    sh = sg.dHT - self.dHT
                    sg.cmap.offset -= sg.cmap.b * sh
                    self.offset_shifts.append(sh)
        self.cmap = SegmentedClock([s.hi for s in self.segs[:-1]], [s.cmap for s in self.segs], ov)
        self.sigma_HT = float(np.sqrt(acc.get("sHT2_w") / nm)) if nm else float("nan")
        self.n_matched_p1 = int(nm)
        self.warnings = warn_set
        nr = acc.get("n_res")
        self._clock_acc = dict(n_pairs=int(acc.get("n_pairs")), n_outliers=int(acc.get("n_out")),
                               residual_rms_s=float(np.sqrt(acc.get("res2") / nr)) if nr else float("nan"),
                               residual_max_s=acc.get("res_max"), n_piecewise_segments=int(acc.get("piecewise")),
                               uniqueness_max=u_max)
        self.timing["pass1"] = time.perf_counter() - t0

    # -------------------------------------------------------------------- Durchgang 2
    def pass2(self, g: h5py.Group | None, mode_offset: float | None) -> None:
        """Endgültige Zuordnung mit ``SegmentedClock``; schreibt (falls ``g``) die Datensätze wie ``write_sync``."""
        from .pipeline import _herald_triggered, _triggered
        t0 = time.perf_counter()
        run, p, cp = self.run, self.p, self.cp
        hr, dr = _HarpReader(run, cp.read_s), _DigReader(run, cp.read_s)
        cmap = self.cmap
        herald_run = _herald_triggered(run)
        M = max(cp.context_s, 20 * run.record_length / run.fs)
        dHT = self.dHT if np.isfinite(self.dHT) else 0.0
        W = _Writer(g)
        run_D = tuple(cmap.D(np.array([0.0, run.duration_s])))
        cnt = dict(seg={k: 0 for k in A.SEG_NAMES}, her={k: 0 for k in A.HER_NAMES},
                   rea={k: 0 for k in (A.REASON_BUSY, A.REASON_EDGE, A.REASON_OTHER)}, sec=0, ovl=0)
        acc = _Acc()
        n_rec = n_her_tot = 0
        sig_list, tol_list, toff = [], [], []
        for seg in self.segs:
            lo, hi = seg.lo, seg.hi
            c_lo = (lo - M) if np.isfinite(lo) else -np.inf
            c_hi = (hi + M) if np.isfinite(hi) else np.inf
            dr.until(c_hi if np.isfinite(c_hi) else np.inf)
            idx, trig, first = dr.records(c_lo, c_hi)
            last = self._last(first)
            if np.isfinite(c_lo) and np.isfinite(c_hi):
                hh = cmap.H(np.array([c_lo, c_hi])) + np.array([-cp.herald_margin_s, cp.herald_margin_s])
            else:
                hh = np.array([cmap.H(np.array([c_lo]))[0] - cp.herald_margin_s if np.isfinite(c_lo) else -np.inf,
                               cmap.H(np.array([c_hi]))[0] + cp.herald_margin_s if np.isfinite(c_hi) else np.inf])
            hr.until(hh[1])
            tH, g0 = hr.get("herald", *hh)
            tags, tcls = self._tags(hr, *hh)
            own = (trig >= lo) & (trig < hi)
            self._copy_check(hr, cmap, trig, own, hh, acc)
            if herald_run:
                a = A.assign_heralds(tH, cmap, trig, first, last, run_D, mode_offset=mode_offset, **self._kw(tags, tcls))
                eh = a.hD + dHT
                ownh = (eh >= lo) & (eh < hi)
                sh = a.seg_herald[own]
                hs = a.herald_segment[ownh]
                W.add(seg_class=a.seg_class[own], seg_herald=np.where(sh >= 0, sh + g0, -1),
                      seg_n_secondary=a.seg_n_secondary[own], seg_overlap_prev=a.seg_overlap_prev[own],
                      record_time_H_s=cmap.H(trig[own]), record_time_session_s=cmap.H(trig[own]) + run.t_start_s,
                      herald_class=a.herald_class[ownh], herald_reason=a.herald_reason[ownh],
                      herald_segment=np.where(hs >= 0, idx[np.clip(hs, 0, None)] if len(idx) else -1, -1),
                      herald_D_s=a.hD[ownh])
                for k in A.SEG_NAMES:
                    cnt["seg"][k] += int(np.sum(a.seg_class[own] == k))
                hc, hrsn = a.herald_class[ownh], a.herald_reason[ownh]
                for k in A.HER_NAMES:
                    cnt["her"][k] += int(np.sum(hc == k))
                for k in cnt["rea"]:
                    cnt["rea"][k] += int(np.sum((hc != A.HER_MATCHED) & (hrsn == k)))
                cnt["sec"] += int(np.sum(a.seg_n_secondary[own] > 0))
                cnt["ovl"] += int(np.sum(a.seg_overlap_prev[own]))
                nm = int(np.sum(a.seg_class[own] == A.SEG_MATCHED))
                acc.add(dHT_w=a.dHT * nm, sHT2_w=a.sigma_HT ** 2 * nm, n_m=nm)
                tol_list.append(a.tol)
                if np.isfinite(a.tag_offset):
                    toff.append(a.tag_offset)
                n_her_tot += int(ownh.sum())
            else:
                trg = _triggered(run)
                seg_c = A.classify_periodic(len(trig)) if not trg else np.full(len(trig), A.SEG_ORPHAN, np.int8)
                to = A.tag_segments(cmap, trig, seg_c, seg_c == A.SEG_ORPHAN, tags, tcls, p.tol_min_s, p.k_sigma)
                if np.isfinite(to):
                    toff.append(to)
                pos = np.sort(cmap.D(tH) + (mode_offset if mode_offset is not None else 0.0))
                n, _ = K.count_in_window(pos, first, last)
                eh = cmap.D(tH) + dHT
                n_her_tot += int(np.sum((eh >= lo) & (eh < hi))) if np.isfinite(dHT) else 0
                W.add(seg_class=seg_c[own], seg_n_heralds=n[own].astype(np.int32),
                      record_time_H_s=cmap.H(trig[own]), record_time_session_s=cmap.H(trig[own]) + run.t_start_s)
                for k in A.SEG_NAMES:
                    cnt["seg"][k] += int(np.sum(seg_c[own] == k))
                acc.add(rec_with=np.sum(n[own] > 0), mode_in=np.sum(n[own]))
            n_rec += int(own.sum())
            nlo = (hi - M) if np.isfinite(hi) else hi
            if np.isfinite(nlo):
                dr.drop_before(nlo)
                hr.drop_before(float(cmap.H(np.array([nlo]))[0]) - cp.herald_margin_s - 1e-3)
        hr.until(np.inf)
        W.close()
        nh = hr.dropped["herald"] + len(hr.buf["herald"])
        self.final = dict(n_records=n_rec, n_heralds=int(nh), counts=cnt, acc=acc.d, tol=max(tol_list) if tol_list else float("nan"),
                          tag_offset=float(np.median(toff)) if toff else float("nan"), mode_offset=mode_offset)
        self.assign_warnings = []
        if herald_run:
            c = self._counts()
            nseg = max(n_rec, 1)
            n_tag = c["segments"]["zufallstrigger"] + c["segments"]["zustandstrigger"]
            fm = c["segments"]["zugeordnet"] / max(nseg - n_tag, 1)
            if fm < p.warn_match_fraction and run.dataset_type == "heralded":
                self.assign_warnings.append(f"nur {fm:.1%} der Records zugeordnet")
            if c["segments"]["mehrdeutig"] / nseg > p.warn_ambiguous_fraction:
                self.assign_warnings.append(f"{c['segments']['mehrdeutig']} mehrdeutige Records")
            if c["reasons"]["unerklärt"] / max(nh, 1) > p.warn_unexplained_fraction:
                self.assign_warnings.append(f"{c['reasons']['unerklärt']} Heralds ohne Record und ohne Erklärung (Totzeit/Rand)")
            if n_her_tot != nh:
                self.assign_warnings.append(f"Segmentierung: {n_her_tot} von {nh} Heralds klassifiziert")
        elif _triggered(run):
            nt = cnt["seg"][A.SEG_RANDOM] + cnt["seg"][A.SEG_STATE]
            if nt < p.warn_match_fraction * n_rec:
                self.assign_warnings.append(f"nur {nt}/{n_rec} Records über CH4 gekennzeichnet")
        cc = self._cc
        if cc.get("available") and cc["fraction_confirmed"] < p.warn_copy_fraction:
            self.assign_warnings.append(f"CH2-Kopie bestätigt nur {cc['fraction_confirmed']:.1%} der Records")
        if g is not None:
            g.attrs["run_id"] = run.run_id
            g.attrs["clockmap"] = __import__("json").dumps(self.cmap.to_dict())
        self.timing["pass2"] = time.perf_counter() - t0

    def _copy_check(self, hr, cmap, trig, own, hh, acc) -> None:
        if not hasattr(self, "_cc"):
            self._cc = dict(available=False)
        cp_, _ = hr.get("adq32_trigger_copy", *hh)
        if not (len(hr.buf["adq32_trigger_copy"]) or hr.dropped["adq32_trigger_copy"]) or not len(trig):
            return
        from .pipeline import _triggered
        p = self.p
        cD = np.sort(cmap.D(cp_))
        off, sig, _ = A.trigger_offset(trig, cD, floor=1e-12)
        if not np.isfinite(off):
            return
        tol = max(p.k_sigma * sig, p.tol_min_s)
        if not _triggered(self.run):
            tol = max(tol, 1.5 * float(self.run.device.get("pulse_generator_step_s", 0.0)))
        n, _ = K.count_in_window(cD + off, trig - tol, trig + tol)
        nrec, _ = K.count_in_window(trig, cD + off - tol, cD + off + tol)
        lo, hi = trig[own].min() if own.any() else np.inf, trig[own].max() if own.any() else -np.inf
        ownc = (cD + off >= lo) & (cD + off <= hi)
        c = self._cc
        c["available"] = True
        c["n_copies"] = c.get("n_copies", 0) + int(ownc.sum())
        c["records_confirmed"] = c.get("records_confirmed", 0) + int(np.sum((n >= 1) & own))
        c["_n_own"] = c.get("_n_own", 0) + int(own.sum())
        c["copies_without_record"] = c.get("copies_without_record", 0) + int(np.sum((nrec == 0) & ownc))
        c["offset_s"], c["sigma_s"], c["tol_s"] = off, sig, tol
        c["fraction_confirmed"] = c["records_confirmed"] / max(c["_n_own"], 1)

    def _counts(self) -> dict:
        c = self.final["counts"]
        return dict(segments={A.SEG_NAMES[k]: v for k, v in c["seg"].items()},
                    heralds={A.HER_NAMES[k]: v for k, v in c["her"].items()},
                    reasons={A.REASON_NAMES[k]: v for k, v in c["rea"].items()},
                    segments_with_secondary=c["sec"], segments_overlapping=c["ovl"])

    # -------------------------------------------------------------------- Bericht
    def report(self) -> dict:
        from .pipeline import _herald_triggered, _triggered
        r = self.run
        ca = self._clock_acc
        m0 = self.segs[0].marker
        ref = [s.refine for s in self.segs if s.refine.get("applied")]
        refine = dict(applied=bool(ref), n_segments=len(ref),
                      n_pairs=int(sum(x["n_pairs"] for x in ref)), n_used=int(sum(x["n_used"] for x in ref)),
                      sigma_before_s=float(np.sqrt(np.mean([x["sigma_before_s"] ** 2 for x in ref]))) if ref else None,
                      sigma_after_s=float(np.sqrt(np.mean([x["sigma_after_s"] ** 2 for x in ref]))) if ref else float("nan"),
                      piecewise=any(x["piecewise"] for x in ref)) if ref else dict(applied=False, reason="keine Paare")
        cc = {k: v for k, v in getattr(self, "_cc", dict(available=False)).items() if not k.startswith("_")}
        d = dict(run_id=r.run_id, block=r.block_id, kind=r.kind, dataset_type=r.dataset_type, triggered=_triggered(r),
                 n_records=r.n_records, n_events=self.n_events,
                 marker=dict(n_pairs=ca["n_pairs"], uniqueness=ca["uniqueness_max"], shift=m0["shift"],
                             n_segments=len(self.segs)),
                 clock=dict(self.cmap.to_dict(), model="segmented", n_outliers=ca["n_outliers"],
                            residual_rms_s=ca["residual_rms_s"], residual_max_s=ca["residual_max_s"],
                            sigma_r_linear=float("nan"), W_s=None, n_segments=len(self.segs),
                            n_piecewise_segments=ca["n_piecewise_segments"], segment_s=self.cp.segment_s,
                            overlap_s=self.cp.overlap_s,
                            segment_offset_shift_rms_s=float(np.sqrt(np.mean(np.square(self.offset_shifts))))
                            if self.offset_shifts else None),
                 clock_markers_only=None, refine=refine, copy_check=cc, free_running={}, delta=self.delta,
                 warnings=self.warnings + self.assign_warnings, timing_s=self.timing, chunked=True)
        f = self.final
        if _herald_triggered(r):
            d["assignment"] = dict(self._counts(), dHT_s=self.dHT, sigma_HT_s=self.sigma_HT, tol_s=f["tol"],
                                   tag_offset_s=f["tag_offset"])
        else:
            n = max(f["n_records"], 1)
            d["free_running"] = dict(n_heralds=f["n_heralds"], records_with_herald=int(f["acc"].get("rec_with", 0)),
                                     herald_mode_in_record=int(f["acc"].get("mode_in", 0)),
                                     fraction_records_with_herald=f["acc"].get("rec_with", 0) / n,
                                     mode_offset_s=f["mode_offset"],
                                     segments={A.SEG_NAMES[k]: v for k, v in f["counts"]["seg"].items()},
                                     tag_offset_s=f["tag_offset"])
        return d


class _Writer:
    """Erweiterbare Datensätze in einer h5-Gruppe (oder nichts, wenn ``g`` None)."""

    def __init__(self, g):
        self.g = g

    def add(self, **arrs) -> None:
        if self.g is None:
            return
        for k, v in arrs.items():
            v = np.asarray(v)
            if k not in self.g:
                self.g.create_dataset(k, data=v, maxshape=(None,), chunks=(max(1024, min(len(v), 1 << 16)) or 1024,))
            elif len(v):
                ds = self.g[k]
                n = ds.shape[0]
                ds.resize((n + len(v),))
                ds[n:] = v

    def close(self) -> None:
        pass


class Reservoir:
    """Gleichmäßige Stichprobe (Algorithmus R) sauberer Records für Δ über alle Läufe, höchstens ``n_max``."""

    def __init__(self, n_max: int, seed: int = 1):
        self.n_max = int(n_max)
        self.rng = np.random.default_rng(seed + 7919)
        self.key: list[str] = []
        self.idx = np.empty(0, np.int64)
        self.pos0 = np.empty(0)
        self.keys = np.empty(0, np.int32)
        self.seen = 0

    def add(self, key: str, idx: np.ndarray, pos0: np.ndarray) -> None:
        if key not in self.key:
            self.key.append(key)
        ki = self.key.index(key)
        n = len(idx)
        free = max(0, self.n_max - len(self.idx))
        take = min(free, n)
        if take:
            self.idx = np.concatenate([self.idx, idx[:take]])
            self.pos0 = np.concatenate([self.pos0, pos0[:take]])
            self.keys = np.concatenate([self.keys, np.full(take, ki, np.int32)])
        seen0 = self.seen + take
        rest = np.arange(take, n)
        if len(rest):
            j = self.rng.integers(0, seen0 + (rest - take) + 1)        # Position in 0 … i
            hit = j < self.n_max
            for r, jj in zip(rest[hit], j[hit]):
                self.idx[jj], self.pos0[jj], self.keys[jj] = idx[r], pos0[r], ki
        self.seen += n

    def sources(self, key: str) -> tuple[np.ndarray, np.ndarray]:
        if key not in self.key:
            return np.empty(0, np.int64), np.empty(0)
        m = self.keys == self.key.index(key)
        o = np.argsort(self.idx[m], kind="stable")
        return self.idx[m][o], self.pos0[m][o]
