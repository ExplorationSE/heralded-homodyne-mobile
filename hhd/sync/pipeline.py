"""Synchronisation einer Sitzung bzw. Kampagne: Marker → Uhrabbildung → Zuordnung → Verfeinerung → Δ → Bericht.

Eingang ausschließlich Messdaten (``hhd.io.session``): PTU, ADQ32-HDF5, Manifest (Whitelist). Die Wahrheitsdatei
wird nie geöffnet; der Vergleich mit der Wahrheit liegt in ``hhd.sim.sync_validate``.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from .. import __version__
from ..io.session import RunData, SessionData, load_campaign, load_session, is_campaign
from . import assign as A
from . import delay as Dl
from . import kernels as K
from .clockmap import ClockMap, Piece, fit_clock, fit_piecewise_auto
from .markers import match_markers

SYNC_SCHEMA = "hhd-sync/1.0"


@dataclass
class SyncParams:
    """Auswerteparameter (keine Wahrheit): Modenform/-breite aus der Quellcharakterisierung, ADQ32-Rearm aus
    dem Datenblatt, Toleranzen."""
    mode_shape: str = "double_exponential"
    gamma: float = 1 / 40e-9
    rearm_s: float = 20e-9
    marker_period_s: float | None = None
    tol_min_s: float = 2e-9
    k_sigma: float = 6.0
    allow_piecewise: bool = True
    refine: bool = True
    calibrate_delay: bool = True
    delay_max_records: int = 20000
    vacuum_max_records: int = 3000
    n_boot: int = 100
    coarse_scan_s: tuple = (-700e-9, 1100e-9, 8e-9)
    fine_half_s: float = 120e-9
    fine_step_s: float = 1e-9
    seed: int = 1
    # Warnschwellen
    warn_uniqueness: float = 0.5
    warn_match_fraction: float = 0.9
    warn_unexplained_fraction: float = 0.01
    warn_ambiguous_fraction: float = 1e-3
    warn_copy_fraction: float = 0.99


def _herald_triggered(run: RunData) -> bool:
    """Trigger am ADQ32 kommt vom Herald (heraldiert bzw. K-H)."""
    return _triggered(run) and run.dataset_type in ("heralded", "herald_blocked")


def _triggered(run: RunData) -> bool:
    src = str(run.attrs.get("adq32_trigger_source", "")).upper()
    if src:
        return src == "TRIG"
    return run.dataset_type in ("heralded", "herald_blocked", "random_trigger")


@dataclass
class RunSync:
    run: RunData
    cmap: ClockMap
    marker: dict
    fit: dict
    tm: tuple                                   # (tH, TD, residual, used)
    assignment: A.Assignment | None = None
    refine: dict = field(default_factory=dict)
    copy_check: dict = field(default_factory=dict)
    free: dict = field(default_factory=dict)
    delta: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)          # Marker/Uhr
    assign_warnings: list = field(default_factory=list)   # Zuordnung/CH2 (je Durchgang neu)
    timing: dict = field(default_factory=dict)
    n_events: int = 0
    marker_map: ClockMap | None = None                     # Abbildung nur aus Markern (vor der Verfeinerung)

    @property
    def key(self) -> str:
        return self.run.block_id

    def windows(self):
        r = self.run
        first = r.first_sample_D()
        return r.trigger_D(), first, first + (r.record_length - 1) / r.fs

    def report(self) -> dict:
        r = self.run
        tH, TD, res, used = self.tm
        d = dict(run_id=r.run_id, block=r.block_id, kind=r.kind, dataset_type=r.dataset_type,
                 triggered=_triggered(r), n_records=r.n_records, n_events=self.n_events, marker=self.marker,
                 clock=dict(self.cmap.to_dict(), model=self.fit["model"], n_outliers=self.fit["n_outliers"],
                            residual_rms_s=float(np.std(res[used])) if used.sum() > 1 else float("nan"),
                            residual_max_s=float(np.max(np.abs(res[used]))) if used.any() else float("nan"),
                            sigma_r_linear=self.fit["sigma_r_linear"], W_s=self.fit.get("W")),
                 clock_markers_only=(self.marker_map or self.cmap).to_dict(),
                 refine=self.refine, copy_check=self.copy_check,
                 free_running={k: v for k, v in self.free.items() if not k.startswith('_')}, delta=self.delta,
                 warnings=self.warnings + self.assign_warnings, timing_s=self.timing)
        if self.assignment is not None:
            a = self.assignment
            d["assignment"] = dict(a.counts(), dHT_s=a.dHT, sigma_HT_s=a.sigma_HT, tol_s=a.tol,
                                   tag_offset_s=a.tag_offset)
        return d


# ---------------------------------------------------------------------------------------------- Bausteine
def sync_clock(run: RunData, p: SyncParams) -> RunSync:
    t0 = time.perf_counter()
    h = run.harp
    tHm = h.role_s("sync_marker")
    on_input = "sync_marker" in run.marker_roles.values() and "sync_marker" not in run.channel_roles.values()
    TDm = run.markers_D()
    mm = match_markers(tHm, TDm, p.marker_period_s)
    warn = list(mm.warnings)
    floor = 1e-12
    cmap, info = fit_clock(mm.TD, mm.tH, allow_piecewise=p.allow_piecewise, floor=floor)
    marker = dict(mm.info(), source="Marker-Eingang" if on_input else "Timing-Kanal")
    n_ev = int(sum(len(v) for v in h.channels.values()) + sum(len(v) for v in h.markers.values()) + len(TDm)
               + run.n_records)
    rs = RunSync(run, cmap, marker, info, (mm.tH, mm.TD, info["residuals"], info["used"]), warnings=warn, n_events=n_ev)
    rs.timing["clock"] = time.perf_counter() - t0
    return rs


def _refine(rs: RunSync, p: SyncParams, tH: np.ndarray, trig: np.ndarray) -> None:
    """Verfeinerung der Abbildung mit Herald/Trigger-Paaren (Architektur A, Vorbericht 2 §4)."""
    a = rs.assignment
    m = a.seg_herald >= 0
    if m.sum() < 20:
        rs.refine = dict(applied=False, reason="zu wenige Paare")
        return
    x = trig[m]
    r = tH[a.seg_herald[m]] - rs.cmap.H(x)          # Harp-Herald − abgebildeter Trigger
    lin = K.robust_linear(x, r, clip=5.0, floor=1e-12)
    med = float(np.median(r[lin["used"]]))
    cm = rs.cmap
    rs.marker_map = ClockMap.from_dict(cm.to_dict())
    old_sigma_a0 = float(np.sqrt(cm.cov[0, 0]))
    sigma_r_before = float(np.std(r[lin["used"]] - med))
    # Steigung: inverse-varianzgewichtete Kombination Marker ⊕ Paare (Paare enthalten die Steigungsabweichung
    # gegenüber der Markerabbildung als lin["b"])
    vm, vp = float(cm.cov[1, 1]), float(lin["cov"][1, 1])
    w = vm / (vm + vp) if (vm + vp) > 0 else 0.0
    db = w * lin["b"]
    corr = lambda X: lin["a0"] + db * (X - lin["x0"])
    cm.a0 = cm.a0 + lin["a0"] + db * (cm.x0 - lin["x0"]) - med
    cm.b = cm.b + db
    cm.cov = np.array([[cm.cov[0, 0], 0.0], [0.0, vm * vp / (vm + vp) if (vm + vp) > 0 else 0.0]])
    res = r - corr(x)
    piece = None
    if p.allow_piecewise:
        piece, _ = fit_piecewise_auto(x[lin["used"]], res[lin["used"]], source="herald_pairs")
        if piece is not None:
            cm.pieces.append(piece)
            res = res - piece(x)
    used = lin["used"]
    rs.refine = dict(applied=True, n_pairs=int(m.sum()), n_used=int(used.sum()), offset_removed_s=med,
                     slope_correction=db, slope_weight=w, sigma_before_s=sigma_r_before,
                     sigma_after_s=float(np.std(res[used])), piecewise=piece is not None,
                     W_s=piece.W if piece is not None else None, sigma_a0_markers_s=old_sigma_a0)


def _copy_check(rs: RunSync, trig: np.ndarray, p: SyncParams) -> None:
    """CH2 (Kopie aller TRIG-Flanken bzw. periodischer Trigger) gegen die ADQ32-Zeitstempel."""
    cp = rs.run.harp.role_s("adq32_trigger_copy")
    if len(cp) == 0 or len(trig) == 0:
        rs.copy_check = dict(available=False)
        return
    cD = np.sort(rs.cmap.D(cp))
    off, sig, _ = A.trigger_offset(trig, cD, floor=1e-12)
    tol = max(p.k_sigma * sig, p.tol_min_s)
    if not _triggered(rs.run):
        # periodische Trigger: Pulsgenerator-Raster (6,4 ns) zwischen Record-Zeitstempel und Kopie
        tol = max(tol, 1.5 * float(rs.run.device.get("pulse_generator_step_s", 0.0)))
    n, _ = K.count_in_window(cD + off, trig - tol, trig + tol)
    ok = n >= 1
    nrec, _ = K.count_in_window(trig, cD + off - tol, cD + off + tol)
    rs.copy_check = dict(available=True, n_copies=int(len(cp)), offset_s=off, sigma_s=sig, tol_s=tol,
                         records_confirmed=int(ok.sum()), fraction_confirmed=float(ok.mean()),
                         copies_without_record=int(np.sum(nrec == 0)))
    if ok.mean() < p.warn_copy_fraction:
        rs.assign_warnings.append(f"CH2-Kopie bestätigt nur {ok.mean():.1%} der Records")


def sync_assign(rs: RunSync, p: SyncParams, mode_offset: float | None = None) -> None:
    run = rs.run
    t0 = time.perf_counter()
    rs.assign_warnings = []
    trig, first, last = rs.windows()
    _copy_check(rs, trig, p)
    tH = np.sort(run.harp.role_s("herald"))
    run_D = tuple(rs.cmap.D(np.array([0.0, run.duration_s])))
    pre = -float(run.device.get("horizontal_offset", 0)) / run.fs
    post = run.record_length / run.fs - pre
    tags, tag_cls = None, A.SEG_RANDOM
    for role, cls in (("random_tag", A.SEG_RANDOM), ("state_tag", A.SEG_STATE)):
        t = run.harp.role_s(role)
        if len(t):
            tags, tag_cls = np.sort(t), cls
            break
    if not _herald_triggered(run):
        # Lauf B / K-V (periodisch) bzw. Zufalls-/Zustandstrigger: keine Herald-Zuordnung, aber Zeitachse,
        # Kennung der Trigger über CH4 und Zahl der Heralds, deren Mode im Record liegt
        seg = A.classify_periodic(run.n_records) if not _triggered(run) else np.full(run.n_records, A.SEG_ORPHAN, np.int8)
        toff = A.tag_segments(rs.cmap, trig, seg, seg == A.SEG_ORPHAN, tags, tag_cls, p.tol_min_s, p.k_sigma)
        pos = rs.cmap.D(tH) + (mode_offset if mode_offset is not None else 0.0)
        n, _ = K.count_in_window(np.sort(pos), first, last)
        rs.free = dict(n_heralds=int(len(tH)), records_with_herald=int(np.sum(n > 0)),
                       herald_mode_in_record=int(n.sum()), fraction_records_with_herald=float(np.mean(n > 0)) if len(n) else 0.0,
                       mode_offset_s=mode_offset, segments={A.SEG_NAMES[k]: int(np.sum(seg == k)) for k in A.SEG_NAMES},
                       tag_offset_s=toff)
        rs.free["_n_in_record"] = n
        rs.free["_seg_class"] = seg
        if _triggered(run):
            nt = int(np.sum(seg == tag_cls))
            if nt < p.warn_match_fraction * len(seg):
                rs.assign_warnings.append(f"nur {nt}/{len(seg)} Records über CH4 gekennzeichnet")
        rs.timing["assign"] = time.perf_counter() - t0
        return
    kw = dict(rearm=p.rearm_s + 1.0 / run.fs, pretrig_s=pre, post_s=post, tol_min=p.tol_min_s, k_sigma=p.k_sigma, tags=tags,
              tag_class=tag_cls)
    rs.assignment = A.assign_heralds(tH, rs.cmap, trig, first, last, run_D, mode_offset=mode_offset, **kw)
    if p.refine and not rs.refine.get("applied"):
        _refine(rs, p, tH, trig)
        if rs.refine.get("applied"):
            rs.assignment = A.assign_heralds(tH, rs.cmap, trig, first, last, run_D, mode_offset=mode_offset, **kw)
    a = rs.assignment
    c = a.counts()
    nseg = max(len(trig), 1); nh = max(len(tH), 1)
    n_tag = c["segments"]["zufallstrigger"] + c["segments"]["zustandstrigger"]
    fm = c["segments"]["zugeordnet"] / max(nseg - n_tag, 1)
    if fm < p.warn_match_fraction and run.dataset_type == "heralded":
        rs.assign_warnings.append(f"nur {fm:.1%} der Records zugeordnet")
    if c["segments"]["mehrdeutig"] / nseg > p.warn_ambiguous_fraction:
        rs.assign_warnings.append(f"{c['segments']['mehrdeutig']} mehrdeutige Records")
    if c["reasons"]["unerklärt"] / nh > p.warn_unexplained_fraction:
        rs.assign_warnings.append(f"{c['reasons']['unerklärt']} Heralds ohne Record und ohne Erklärung (Totzeit/Rand)")
    rs.timing["assign"] = time.perf_counter() - t0


# ---------------------------------------------------------------------------------------------- Δ
def _vacuum_blocks(sess: SessionData, p: SyncParams) -> tuple[list, str]:
    out, n = [], 0
    for r in sess.runs_of("vacuum"):
        for _, blk in r.iter_records(chunk=512):
            out.append(blk); n += len(blk)
            if n >= p.vacuum_max_records:
                return out, "K-V"
    return out, "K-V"


def calibrate_delay(sess: SessionData, syncs: dict[str, RunSync], p: SyncParams) -> dict:
    t0 = time.perf_counter()
    fs = float(sess.device["sampling_rate_Hz"])
    kern = Dl.mode_kernel(p.mode_shape, p.gamma, fs)
    blocks, src = _vacuum_blocks(sess, p)
    if not blocks:
        # Ersatz: frühe Pretrigger-Samples der heraldierten Records (vor der Mode)
        src = "Pretrigger"
        for rs in syncs.values():
            if rs.run.dataset_type == "heralded" and rs.run.n_records:
                pre = int(-float(sess.device.get("horizontal_offset", 0)))
                cut = max(pre - int(6 / p.gamma * fs), len(kern) + 64)
                blocks.append(rs.run.read_records(np.arange(min(rs.run.n_records, 1000)), cols=slice(0, cut)))
                break
    norm = Dl.vacuum_norm(blocks, kern, source=src)
    if norm is None:
        return dict(available=False, reason="keine Normierungsdaten")
    cand = []
    for rs in syncs.values():
        a = rs.assignment
        if rs.run.dataset_type != "heralded" or a is None:
            continue
        ok = (a.seg_class == A.SEG_MATCHED) & (a.seg_n_secondary == 0) & ~a.seg_overlap_prev
        cand.append((rs, np.flatnonzero(ok)))
    n_tot = sum(len(ix) for _, ix in cand)
    if n_tot < 50:
        return dict(available=False, reason=f"zu wenige saubere Records ({n_tot})", norm=asdict(norm))
    frac = min(1.0, p.delay_max_records / n_tot)
    rng = np.random.default_rng(p.seed)
    sources = []
    for rs, ix in cand:
        if frac < 1:
            ix = np.sort(rng.choice(ix, int(round(len(ix) * frac)), replace=False))
        trig, first, _ = rs.windows()
        pos0 = (trig[ix] - first[ix]) * fs
        sources.append((rs.run.read_records, ix, pos0))
    res = Dl.calibrate(sources, fs, kern, norm, p.gamma, p.mode_shape, coarse=p.coarse_scan_s,
                       fine_half=p.fine_half_s, fine_step=p.fine_step_s, n_boot=p.n_boot, seed=p.seed)
    dT = res["delta"]; sT = res["sigma_boot"]
    per_run = {}
    for rs, _ in cand:
        a = rs.assignment
        se_dHT = a.sigma_HT / np.sqrt(max(np.sum(a.seg_class == A.SEG_MATCHED), 1))
        rs.delta = dict(delta_s=dT + a.dHT, sigma_s=float(np.hypot(sT, se_dHT)), delta_T_s=dT, dHT_s=a.dHT)
        per_run[rs.key] = rs.delta
    out = dict(available=True, delta_T_s=dT, sigma_delta_T_s=sT, sigma_fit_s=res["sigma_fit"], A=res["A"],
               c=res["c"], contrast=res["contrast"], sigma_A=res["sigma_A"], n_records=res["n_records"],
               n_boot=res["n_boot"], norm=asdict(norm), coarse_peak_s=res["coarse"]["peak"], per_run=per_run,
               delta_mean_s=float(np.mean([v["delta_s"] for v in per_run.values()])),
               scan=dict(grid_s=res["grid"].tolist(), V=res["V"].tolist()),
               coarse=dict(grid_s=res["coarse"]["grid"].tolist(), V=res["coarse"]["V"].tolist()),
               warnings=[], time_s=time.perf_counter() - t0)
    if not (res["A"] > 3 * max(res["sigma_A"], 1e-12)):
        out["warnings"].append(f"Varianzmaximum nicht signifikant (A = {res['A']:.3g} ± {res['sigma_A']:.2g})")
    g = res["grid"]
    if abs(dT - g[0]) < 5e-9 or abs(dT - g[-1]) < 5e-9:
        out["warnings"].append("Δ am Rand des Feinrasters")
    return out


# ---------------------------------------------------------------------------------------------- Sitzung
def sync_session(path: str | Path | SessionData, params: SyncParams | None = None, out: str | Path | None = None,
                 write: bool = True) -> dict:
    p = params or SyncParams()
    t_all = time.perf_counter()
    sess = path if isinstance(path, SessionData) else load_session(path)
    syncs: dict[str, RunSync] = {}
    for run in sess.runs:
        rs = sync_clock(run, p)
        sync_assign(rs, p)
        syncs[rs.key] = rs
    delta = dict(available=False, reason="abgeschaltet")
    if p.calibrate_delay:
        delta = calibrate_delay(sess, syncs, p)
    # endgültige Klassifikation mit Modenort (sekundär) bzw. Heralds je Fenster für Lauf B / K-V
    if delta.get("available"):
        dmean = delta["delta_mean_s"]
        for rs in syncs.values():
            off = rs.delta.get("delta_s", dmean) if _herald_triggered(rs.run) else dmean
            sync_assign(rs, p, mode_offset=off)
    elapsed = time.perf_counter() - t_all
    n_ev = sum(rs.n_events for rs in syncs.values())
    rep = dict(schema=SYNC_SCHEMA, software=f"hhd {__version__}", backend="python", session=str(sess.path),
               session_id=sess.session_id, campaign_id=sess.campaign_id, calibration_id=sess.calibration_id,
               params={k: v for k, v in asdict(p).items()}, import_warnings=sess.warnings,
               runs={k: rs.report() for k, rs in syncs.items()}, delta=delta,
               performance=dict(elapsed_s=elapsed, n_events=n_ev, events_per_s=n_ev / elapsed if elapsed else 0.0))
    rep["quality"] = quality_summary(rep)
    if write:
        o = Path(out) if out else sess.path / "sync"
        write_sync(o, rep, syncs)
        rep["output"] = str(o)
    rep["_syncs"] = syncs
    return rep


def quality_summary(rep: dict) -> dict:
    """Selbstprüfung: Zusammenfassung über Läufe, Ampel und Warnliste."""
    W = []
    for k, r in rep["runs"].items():
        W += [f"{k}: {w}" for w in r["warnings"]]
    W += [f"Δ: {w}" for w in rep["delta"].get("warnings", [])]
    W += [f"Import: {w}" for w in rep.get("import_warnings", [])]
    seg = {}; her = {}; rea = {}
    for r in rep["runs"].values():
        a = r.get("assignment")
        if a:
            for d, src in ((seg, a["segments"]), (her, a["heralds"]), (rea, a["reasons"])):
                for kk, v in src.items():
                    d[kk] = d.get(kk, 0) + v
    trig = [r for r in rep["runs"].values() if r.get("assignment")]
    res = dict(n_runs=len(rep["runs"]), n_warnings=len(W), warnings=W, segments=seg, heralds=her, reasons=rea,
               marker_sigma_r_max_s=max((r["clock"]["sigma_r"] for r in rep["runs"].values()), default=float("nan")),
               marker_uniqueness_max=max((r["marker"]["uniqueness"] for r in rep["runs"].values()), default=float("nan")),
               pair_sigma_max_s=max((r["refine"].get("sigma_after_s", float("nan")) for r in trig), default=float("nan")),
               status="ok" if not W else "warnung")
    return res


def _json_default(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, Path):
        return str(o)
    raise TypeError(type(o))


def write_sync(out: Path, rep: dict, syncs: dict[str, RunSync]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    js = {k: v for k, v in rep.items() if not k.startswith("_")}
    (out / "sync.json").write_text(json.dumps(js, indent=1, ensure_ascii=False, default=_json_default))
    with h5py.File(out / "sync.h5", "w") as f:
        f.attrs["schema"] = SYNC_SCHEMA
        f.attrs["software"] = rep["software"]
        f.attrs["session_id"] = rep["session_id"]
        f.attrs["seg_classes"] = json.dumps(A.SEG_NAMES, ensure_ascii=False)
        f.attrs["herald_classes"] = json.dumps(A.HER_NAMES, ensure_ascii=False)
        f.attrs["reasons"] = json.dumps(A.REASON_NAMES, ensure_ascii=False)
        for k, rs in syncs.items():
            g = f.create_group(f"runs/{k}")
            g.attrs["run_id"] = rs.run.run_id
            g.attrs["clockmap"] = json.dumps(rs.cmap.to_dict())
            trig, _, _ = rs.windows()
            g["record_time_H_s"] = rs.cmap.H(trig)                        # Zeitachse in Harp-Zeit des Laufs
            g["record_time_session_s"] = rs.cmap.H(trig) + rs.run.t_start_s
            tH, TD, res, used = rs.tm
            mg = g.create_group("markers")
            mg["tH_s"] = tH; mg["TD_s"] = TD; mg["residual_s"] = res; mg["used"] = used
            a = rs.assignment
            if a is not None:
                for name in ("seg_class", "seg_herald", "seg_n_secondary", "seg_overlap_prev", "herald_class",
                             "herald_reason", "herald_segment"):
                    g[name] = getattr(a, name)
                g["herald_D_s"] = a.hD
            elif "_n_in_record" in rs.free:
                g["seg_class"] = rs.free["_seg_class"]
                g["seg_n_heralds"] = rs.free["_n_in_record"].astype(np.int32)


def sync_campaign(path: str | Path, params: SyncParams | None = None, out: str | Path | None = None) -> dict:
    camp = load_campaign(path)
    reps = {}
    for s in camp.sessions:
        o = (Path(out) / s.session_id) if out else None
        r = sync_session(s, params, o)
        r.pop("_syncs", None)
        reps[s.session_id] = r
    summ = dict(n_sessions=len(reps), n_warnings=sum(r["quality"]["n_warnings"] for r in reps.values()),
                delta_T_s={k: r["delta"].get("delta_T_s") for k, r in reps.items()},
                events_per_s=float(np.mean([r["performance"]["events_per_s"] for r in reps.values()])) if reps else 0.0)
    return dict(campaign=str(path), sessions=reps, summary=summ)


def sync_any(path: str | Path, params: SyncParams | None = None, out: str | Path | None = None) -> dict:
    return sync_campaign(path, params, out) if is_campaign(path) else sync_session(path, params, out)
