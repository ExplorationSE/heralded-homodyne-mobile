"""Validierung der Synchronisation gegen die Simulator-Wahrheit (``truth.h5``) – nur Tests und ``hhd validate``.

Kennzahlen je Lauf:

* Markerpaare: Anteil korrekt gepaarter Marker (gleicher wahrer Marker auf beiden Seiten).
* Pulls für a, b: Markerfit gegen den LS-Fit der idealen (rauschfreien, aber quantisierten) Markerpunkte
  t_H = t_m − t0 + d_MH (+ Marker-Latenz am Marker-Eingang), T_D = ⌊clk(t_m + d_MD)/r⌋·r.
* Abbildungsfehler über wahre Zeiten (Heralds): e = H(clk(t)) − (t − t0 + d_MH − d_MD); Mittelwert (enthält die
  Quantisierungsphase der SYNC-Zeitstempel bzw. die Marker-Latenz) und Streuung.
* Fehlzuordnungsrate: zugeordnete Records mit falschem Herald; Verluste (Herald-Record nicht zugeordnet);
  Klassenkonsistenz (Zufalls-/Zustandstrigger, Heralds ohne eigenen Record nicht „zugeordnet").
* Δ-Pull: (Δ_fit − Δ_wahr)/σ mit Δ_wahr = ⟨c_D − h_D⟩ über zugeordnete, nicht dunkle Heralds (end-to-end, mit
  der gefitteten Abbildung); Δ_T,wahr = ⟨c_D − T_trig⟩.
"""
from __future__ import annotations

import json
from pathlib import Path

import h5py
import numpy as np

from ..io.session import load_session, is_campaign, load_campaign
from ..sync import assign as A
from ..sync.clockmap import ClockMap
from .config import SimConfig
from .digitizer import TRIG_HERALD, TRIG_RANDOM, TRIG_STATE

MODE_HERALDED = 0


class Truth:
    def __init__(self, session_dir: str | Path):
        d = Path(session_dir)
        self.f = h5py.File(d / "truth.h5", "r")
        man = json.loads((d / "session.json").read_text(encoding="utf-8"))
        self.cfg = SimConfig.from_dict(man["config"])
        ck = self.f["clock"]
        self.eps, self.t_arm = float(ck.attrs["eps"]), float(ck.attrs["t_arm_s"])
        self.rw = float(ck.attrs.get("rw_D", 0.0))
        self.tg, self.phig = ck["t_grid"][:], ck["phi"][:]

    def clk(self, t):
        return (1 + self.eps) * (np.asarray(t) - self.t_arm) + np.interp(t, self.tg, self.phig)

    def block(self, bid):
        return self.f["blocks"][bid]

    def close(self):
        self.f.close()


def _pull(x, x0, s):
    return float((x - x0) / s) if s and np.isfinite(s) and s > 0 else float("nan")


def validate_run(T: Truth, rs, sess) -> dict:
    run = rs.run
    g = T.block(run.block_id)
    t0 = float(g.attrs["t0_true_s"])
    cfg = T.cfg
    d_MH, d_MD = cfg.markers.d_MH, cfg.digitizer.d_MD
    on_input = not cfg.markers.on_timing_channel
    lat = cfg.harp.marker_offset if on_input else 0.0
    out: dict = {}
    # ---- Markerpaare
    tH, TD, res, used = rs.tm
    mk = g["markers"]
    m_ps = mk["harp_ps"][:]; m_rec = mk["recorded_by_digitizer"][:]; m_t = mk["t_true"][:]
    order = np.argsort(m_ps)
    hp = np.rint(tH * 1e12).astype(np.int64)
    jh = np.searchsorted(m_ps[order], hp)
    jh = order[np.clip(jh, 0, len(order) - 1)]
    okh = m_ps[jh] == hp
    rec_idx = np.flatnonzero(m_rec)                          # D-Marker k ↔ wahrer Marker rec_idx[k]
    allD = run.markers_D()
    kd = np.clip(np.searchsorted(allD, TD - 1e-13), 0, len(allD) - 1)
    jd = rec_idx[np.clip(kd, 0, len(rec_idx) - 1)] if len(rec_idx) == len(allD) else -np.ones(len(TD), np.int64)
    correct = okh & (jh == jd)
    out["markers"] = dict(n_pairs=int(len(tH)), correct=int(correct.sum()), fraction_correct=float(correct.mean()) if len(tH) else float("nan"),
                          n_true=int(len(m_t)))
    # ---- Pulls a, b (Markerfit gegen idealen LS-Fit)
    r = float(sess.device["sync_resolution_s"])
    sel = m_rec & (m_ps >= 0)
    tH_i = m_t[sel] - t0 + d_MH + lat - 0.5 * cfg.harp.resolution    # Harp: ⌊t/1 ps⌋
    TD_i = np.floor(T.clk(m_t[sel] + d_MD) / r + 1e-9) * r
    mm = ClockMap.from_dict(rs.report()["clock_markers_only"])
    if len(TD_i) > 2:
        X = np.vstack([np.ones_like(TD_i), TD_i - mm.x0]).T
        c, *_ = np.linalg.lstsq(X, tH_i, rcond=None)
        rep = rs.report()["clock_markers_only"]
        b_true = 1.0 / (1.0 + T.eps)
        out["clock"] = dict(a0_fit=mm.a0, a0_ideal=float(c[0]), b_fit=mm.b, b_ideal=float(c[1]), b_true=b_true,
                            pull_a=_pull(mm.a0, c[0], rep["sigma_a0"]), pull_b=_pull(mm.b, c[1], rep["sigma_b"]),
                            pull_b_true=_pull(mm.b, b_true, rep["sigma_b"]) if T.rw == 0 else float("nan"),
                            sigma_a0=rep["sigma_a0"], sigma_b=rep["sigma_b"], piecewise=bool(rep["pieces"]))
    # ---- Abbildungsfehler über wahre Zeiten (Heralds, alle)
    he = g["heralds"]
    t_det = he["t_det"][:]; h_ps = he["harp_ps"][:]
    if len(t_det):
        e = rs.cmap.H(T.clk(t_det)) - (t_det - t0 + d_MH + lat - d_MD)
        e_m = mm.H(T.clk(t_det)) - (t_det - t0 + d_MH + lat - d_MD)
        out["mapping"] = dict(mean_s=float(np.mean(e)), rms_s=float(np.std(e)), max_dev_s=float(np.max(np.abs(e - e.mean()))),
                              markers_only_rms_s=float(np.std(e_m)), markers_only_max_dev_s=float(np.max(np.abs(e_m - e_m.mean()))))
    # ---- Zuordnung
    rec = g["records"]
    r_kind = rec["kind"][:]; r_src = rec["source_index"][:]
    h_rec = he["record"][:]; is_dark = he["is_dark"][:]
    a = rs.assignment
    seg_cls = a.seg_class if a is not None else rs.free.get("_seg_class")
    if a is not None:
        ours_ps = np.sort(run.harp.role_ps("herald"))
        # unser Herald-Index → wahrer Herald-Index
        valid = np.flatnonzero(h_ps >= 0)
        o = valid[np.argsort(h_ps[valid])]
        jj = np.searchsorted(h_ps[o], ours_ps)
        ours2true = o[np.clip(jj, 0, len(o) - 1)]
        ours2true = np.where(h_ps[ours2true] == ours_ps, ours2true, -1)
        matched = a.seg_herald >= 0
        true_h = np.where(r_kind == TRIG_HERALD, r_src, -2)
        mis = matched & (ours2true[np.clip(a.seg_herald, 0, None)] != true_h)
        should = r_kind == TRIG_HERALD
        lost = should & ~matched
        out["assignment"] = dict(n_records=int(len(r_kind)), n_matched=int(matched.sum()), misassigned=int(mis.sum()),
                                 misassignment_rate=float(mis.sum() / max(matched.sum(), 1)),
                                 herald_records=int(should.sum()), lost=int(lost.sum()),
                                 lost_ambiguous=int(np.sum(lost & (a.seg_class == A.SEG_AMBIGUOUS))),
                                 efficiency=float((matched & ~mis).sum() / max(should.sum(), 1)))
        # Heralds ohne eigenen Record dürfen nicht „zugeordnet" sein
        no_rec = h_rec < 0
        tr2ours = -np.ones(len(h_ps), np.int64)
        ok = ours2true >= 0
        tr2ours[ours2true[ok]] = np.flatnonzero(ok)
        idx = tr2ours[no_rec & (tr2ours >= 0)]
        out["assignment"]["no_record_but_matched"] = int(np.sum(a.herald_class[idx] == A.HER_MATCHED))
        out["assignment"]["no_record_reason"] = {A.REASON_NAMES[k]: int(np.sum(a.herald_reason[idx] == k))
                                                 for k in (A.REASON_BUSY, A.REASON_EDGE, A.REASON_OTHER)}
        # ---- Δ
        mo = g["modes"]
        mk_ = mo["kind"][:] == MODE_HERALDED
        pair2c = dict(zip(mo["source_index"][:][mk_].tolist(), mo["centre_D"][:][mk_].tolist()))
        pairs = he["pair"][:]
        sel_r = np.flatnonzero(matched & ~mis)
        hi = a.seg_herald[sel_r]; ti = ours2true[hi]
        good = ~is_dark[ti]
        cD = np.array([pair2c.get(int(pp), np.nan) for pp in pairs[ti[good]]])
        if np.isfinite(cD).any():
            hD = a.hD[hi[good]]
            trig = run.trigger_D()[sel_r[good]]
            out["delta_true"] = dict(delta_s=float(np.nanmean(cD - hD)), delta_T_s=float(np.nanmean(cD - trig)),
                                     n=int(np.isfinite(cD).sum()))
            if rs.delta:
                out["delta_true"]["pull"] = _pull(rs.delta["delta_s"], out["delta_true"]["delta_s"], rs.delta["sigma_s"])
                out["delta_true"]["delta_fit_s"] = rs.delta["delta_s"]
    if seg_cls is not None:
        for kind, cls, name in ((TRIG_RANDOM, A.SEG_RANDOM, "random"), (TRIG_STATE, A.SEG_STATE, "state")):
            m = r_kind == kind
            if m.any():
                out.setdefault("tags", {})[name] = dict(n=int(m.sum()), correct=int(np.sum(seg_cls[m] == cls)),
                                                        false_tags=int(np.sum((seg_cls == cls) & ~m)))
    return out


def validate_session(path: str | Path, rep: dict | None = None, params=None) -> dict:
    """Synchronisiert (falls ``rep`` fehlt) und vergleicht mit der Wahrheit."""
    from ..sync.pipeline import sync_session
    if rep is None or "_syncs" not in rep:
        rep = sync_session(path, params, write=False)
    sess = load_session(path)
    T = Truth(path)
    try:
        runs = {k: validate_run(T, rs, sess) for k, rs in rep["_syncs"].items()}
    finally:
        T.close()
    # Gesamt
    tot = dict(n_matched=0, misassigned=0, lost=0, herald_records=0, marker_pairs=0, marker_correct=0)
    pa, pb, pd, mrms = [], [], [], []
    for r in runs.values():
        tot["marker_pairs"] += r["markers"]["n_pairs"]; tot["marker_correct"] += r["markers"]["correct"]
        if "assignment" in r:
            for k in ("n_matched", "misassigned", "lost", "herald_records"):
                tot[k] += r["assignment"][k]
        if "clock" in r:
            pa.append(r["clock"]["pull_a"]); pb.append(r["clock"]["pull_b"])
        if "mapping" in r:
            mrms.append(r["mapping"]["rms_s"])
        if "delta_true" in r and "pull" in r["delta_true"]:
            pd.append(r["delta_true"]["pull"])
    d = rep["delta"]
    dT_true = [runs[k]["delta_true"]["delta_T_s"] for k, rs in rep["_syncs"].items() if "delta_true" in runs[k] and rs.run.dataset_type == "heralded"]
    summ = dict(tot, misassignment_rate=tot["misassigned"] / max(tot["n_matched"], 1),
                efficiency=(tot["n_matched"] - tot["misassigned"]) / max(tot["herald_records"], 1),
                marker_fraction_correct=tot["marker_correct"] / max(tot["marker_pairs"], 1),
                pull_a=_stats(pa), pull_b=_stats(pb), pull_delta=_stats(pd),
                mapping_rms_max_s=float(np.max(mrms)) if mrms else float("nan"))
    if d.get("available") and dT_true:
        t = float(np.mean(dT_true))
        summ["delta_T"] = dict(fit_s=d["delta_T_s"], true_s=t, sigma_s=d["sigma_delta_T_s"],
                               pull=_pull(d["delta_T_s"], t, d["sigma_delta_T_s"]))
    return dict(session=str(path), runs=runs, summary=summ, quality=rep["quality"])


def _stats(x):
    x = np.asarray([v for v in x if np.isfinite(v)])
    if len(x) == 0:
        return dict(n=0)
    return dict(n=int(len(x)), mean=float(x.mean()), std=float(x.std(ddof=1)) if len(x) > 1 else float("nan"),
                max_abs=float(np.max(np.abs(x))))


def validate_any(path: str | Path, params=None) -> dict:
    if is_campaign(path):
        c = load_campaign(path)
        ses = {s.session_id: validate_session(s.path, params=params) for s in c.sessions}
        pa = [v for s in ses.values() for v in [s["summary"].get("delta_T", {}).get("pull", np.nan)]]
        return dict(campaign=str(path), sessions=ses,
                    summary=dict(n_sessions=len(ses), pull_delta_T=_stats(pa),
                                 misassigned=sum(s["summary"]["misassigned"] for s in ses.values()),
                                 n_matched=sum(s["summary"]["n_matched"] for s in ses.values())))
    return validate_session(path, params=params)
