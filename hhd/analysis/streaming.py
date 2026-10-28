"""Auswertung langer Sitzungen mit begrenztem Speicher (Datenstrom-Auswertung).

Gleiche Kette wie :func:`hhd.analysis.pipeline.analyze_session`, aber kein Lauf und keine Gruppe liegt je ganz im
Speicher:

1. **Synchronisation** wie bisher (je Lauf, Harp-Ereignisse nach jedem Lauf freigegeben); gelesen wird danach
   nur ``sync.json`` und je Block ein Ausschnitt von ``sync.h5``.
2. **Durchgang 1** je Lauf in Record-Blöcken: Köpfe, Sync-Ausschnitt, Wellenformen → Rohquadratur, Abdeckung,
   LO-Monitor, Gruppenmasken. Geschrieben wird sofort nach ``analysis/quadratures.h5`` (gleiches Layout wie die
   In-Memory-Auswertung); gehalten werden nur Summen je Lauf (n, Σq … Σq⁴, LO-Monitor).
3. **Kalibrierung** aus diesen Summen (identische Formeln wie :func:`hhd.calib.calibrate.calibrate`).
4. **Durchgang 2**: Quadraturen blockweise aus der Datei lesen, normieren und in **Histogramme** je Lauf und
   Gruppe einsortieren (Breite 1/120, ±8, plus exakte Potenzsummen bis x⁸).
5. **Rekonstruktion** aus den Histogrammen: MaxLik auf Bins (wie ``estimator = "binned"``), η-Fit, W(0,0),
   p_n, Wigner-Funktion; Var x und W(0,0)-Momentenschätzer exakt aus den Potenzsummen.
6. **Bootstrap** als Poisson-Bootstrap der Histogramme (je Lauf), die Vakuumnormierung wird je Ziehung aus den
   K-V-Momenten neu gezogen und als Skalierung x' = c·(x + δ) angewandt (wie ``boot_vacuum``).

Speicher: Block (``chunk`` Records) + Histogramme (≈ 15 kB je Lauf und Gruppe), unabhängig von der Sitzungsdauer.
Unterschiede zur In-Memory-Auswertung: MaxLik stets auf Bins (1/120 breit, bis 0.14: 0,05), KS-Test aus Histogramm-CDFs
(asymptotischer p-Wert), Konfidenzintervalle normal/Perzentil (kein Jackknife/BCa), keine PCA-Prüfung,
``mode_source`` = parametrisch.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import h5py
import numpy as np

from .. import __version__
from ..calib.calibrate import VAC, Calibration, crosstalk_check, drift_vacuum_lo
from ..modes.extract import ModeSpec, adc_offset, project_records, laser_info, resolve_mode_shape, template_bank
from ..recon.core import _iterate, fidelity_diag, lossy_fock1, patterns, photon_stats, wigner_radial
from ..recon.stats import R_GRID, w00_score_coeffs
from ..sync import assign as A
from .pipeline import (CI_QUANT, SCHEMA, AnalysisParams, _clean, _summ, drift_ab_from_per,
                       efficiency_budget, session_state)

LIM, FINE = 8.0, 120                          # Histogramm ±8, Breite 1/120
NB = int(2 * LIM * FINE)
EDGES = np.linspace(-LIM, LIM, NB + 1)
REBIN = 1                                     # MaxLik-Bins 1/120 ≈ 0,0083 (bis 0.14: 6 → 0,05)
KEY = {"heralded": "A", "free_running": "B", "random_trigger": "B", "vacuum": "KV", "herald_blocked": "KH",
       "state": "state", "electronic": "EL"}


# ---------------------------------------------------------------- Histogramm mit exakten Potenzsummen
class QHist:
    """Histogramm normierter Quadraturen + Potenzsummen Σx^k (k ≤ 8); addierbar, konstanter Speicher."""

    def __init__(self):
        self.h = np.zeros(NB, np.int64)
        self.S = np.zeros(9)
        self.n_out = 0
        self.max_abs = 0.0

    def add(self, x: np.ndarray) -> None:
        x = np.asarray(x, float)
        if not len(x):
            return
        i = np.floor((x + LIM) * FINE).astype(np.int64)
        out = (i < 0) | (i >= NB)
        self.n_out += int(out.sum())
        self.h += np.bincount(np.clip(i, 0, NB - 1), minlength=NB)
        p = np.ones_like(x)
        for k in range(9):
            self.S[k] += float(p.sum()); p = p * x
        self.max_abs = max(self.max_abs, float(np.max(np.abs(x))))

    def merge(self, o: "QHist") -> "QHist":
        r = QHist(); r.h = self.h + o.h; r.S = self.S + o.S; r.n_out = self.n_out + o.n_out
        r.max_abs = max(self.max_abs, o.max_abs)
        return r

    @property
    def n(self) -> int:
        return int(self.S[0])

    def moments(self) -> dict:
        n = self.S[0]
        m = self.S / n                                  # Rohmomente E[x^k]
        mu = m[1]
        var = m[2] - mu ** 2
        c4 = m[4] - 4 * mu * m[3] + 6 * mu ** 2 * m[2] - 3 * mu ** 4          # E[(x−μ)^4]
        var_err = np.sqrt(max(c4 - var ** 2, 0.0) * n / (n - 1) / n)
        a = w00_score_coeffs(2)                         # s = a0 + a1 x² + a2 x⁴
        es = a[0] + a[1] * m[2] + a[2] * m[4]
        es2 = (a[0] ** 2 + 2 * a[0] * a[1] * m[2] + (a[1] ** 2 + 2 * a[0] * a[2]) * m[4]
               + 2 * a[1] * a[2] * m[6] + a[2] ** 2 * m[8])
        w_err = np.sqrt(max(es2 - es ** 2, 0.0) * n / (n - 1) / n)
        return dict(n=int(n), mean_x=float(mu), var_x=float(var), var_err=float(var_err), W00_moments=float(es),
                    W00_moments_err=float(w_err))


def merge_all(hs) -> QHist:
    out = QHist()
    for h in hs:
        out = out.merge(h)
    return out


# ---------------------------------------------------------------- Schätzer auf Bins
_SUB = np.linspace(0, 1, 5); _WTS = np.array([1, 4, 2, 4, 1]) / 12.0


def _coarse(counts: np.ndarray) -> np.ndarray:
    return counts.reshape(-1, REBIN).sum(1)


def _bin_patterns(nmax: int, c: float = 1.0, d: float = 0.0) -> np.ndarray:
    """Bin-Mittel der Muster für die groben Bins nach der Abbildung x' = c·(x + d) (Simpson, 5 Stützstellen)."""
    e = EDGES[::REBIN]
    w = (e[1] - e[0]) * c
    xs = (c * (e[:-1] + d))[:, None] + w * _SUB[None, :]
    return (patterns(xs.ravel(), nmax).reshape(nmax + 1, len(e) - 1, 5) * _WTS).sum(-1)


def stats_from_hist(parts, nmax: int = 10, tol: float = 1e-8, p0=None, full: bool = True, exact: dict | None = None):
    """Kenngrößen einer Gruppe aus Histogrammen. ``parts``: Liste (Zählungen fein, c, d) – je Lauf eine Abbildung
    (Bootstrap); ``exact``: Momente aus Potenzsummen (Punktschätzung), sonst aus den Bins."""
    Ps, ws = [], []
    P1s = []
    mom_n = mom_s = None
    for counts, c, d in parts:
        hc = _coarse(counts)
        if not hc.any():
            continue
        Ps.append(_bin_patterns(nmax, c, d)); ws.append(hc.astype(float))
        P1s.append(Ps[-1][:2])
        xc = c * (0.5 * (EDGES[:-1] + EDGES[1:]) + d)
        k = np.arange(9)[:, None]
        sm = (counts[None, :] * xc[None, :] ** k).sum(1)
        mom_s = sm if mom_s is None else mom_s + sm
    P = np.concatenate(Ps, axis=1); w = np.concatenate(ws)
    m = w > 0
    p, it, ok, _ = _iterate(P[:, m], w[m], 2, 20000, tol, p0)
    if exact is None:
        hq = QHist(); hq.S = mom_s
        exact = hq.moments()
    P1 = np.concatenate(P1s, axis=1)
    ef = _eta_fit_binned_parts(P1, w)
    W00 = float(np.sum(p * (-1.0) ** np.arange(len(p))) / np.pi)
    out = dict(exact, W00=W00, p_n=p.tolist(), eta_fit=ef["eta"], eta_fit_err_fisher=ef["sigma_fisher"],
               eta_var=exact["var_x"] - 0.5, n_iter=it, converged=ok, estimator="binned_stream", nmax=nmax)
    if full:
        out.update(photon_stats(p))
        out["fidelity_lossy_fock1"] = fidelity_diag(p, lossy_fock1(ef["eta"], nmax))
        out["fidelity_fock1"] = float(p[1]) if len(p) > 1 else 0.0
        out["wigner_r"] = R_GRID.tolist()
        out["wigner_radial"] = wigner_radial(p, R_GRID).tolist()
    else:
        out["mean_n"] = float(np.dot(np.arange(len(p)), p))
    out["_p"] = p
    return out


def _eta_fit_binned_parts(P1: np.ndarray, w: np.ndarray, max_iter: int = 200) -> dict:
    m = w > 0
    P0, P1_ = P1[0][m], P1[1][m]; ww = w[m]
    e = 0.5
    for _ in range(max_iter):
        pr = (1 - e) * P0 + e * P1_
        d = P1_ - P0
        g = np.sum(ww * d / pr); hh = -np.sum(ww * (d / pr) ** 2)
        e_new = float(np.clip(e - g / hh, 1e-6, 1 - 1e-6))
        if abs(e_new - e) < 1e-10:
            e = e_new; break
        e = e_new
    pr = (1 - e) * P0 + e * P1_
    fisher = float(np.sum(ww * ((P1_ - P0) / pr) ** 2))
    return dict(eta=e, sigma_fisher=float(1 / np.sqrt(fisher)) if fisher > 0 else float("nan"))


# ---------------------------------------------------------------- Durchgang 1: Rohquadraturen je Lauf
class _PCAAcc:
    """Inkrementelle Fensterkovarianz für die PCA (wie ``modes.pca.pca_mode``, gleiche Auswahl und Obergrenze)."""

    def __init__(self, spec, off, cap, half_ns=320.0, B=8, n_var=2000):
        from ..modes.pca import pca_geometry
        self.spec, self.off, self.half_ns, self.B, self.n_var = spec, off, half_ns, B, n_var
        self.half, self.nb = pca_geometry(spec, half_ns, B)
        self.cap = cap if cap and cap > 0 else np.inf
        self.S = {g: [np.zeros(self.nb), np.zeros((self.nb, self.nb)), 0] for g in ("A", "KV")}
        self.var_s = 0.0; self.var_n = 0

    def start_run(self, group):
        """Indexbudget des Laufs (In-Memory: ``idx[:cap − N]`` vor der Fensterprüfung)."""
        if group is None:
            return None
        N = self.S[group][2]
        return dict(group=group, budget=(np.inf if not np.isfinite(self.cap) else max(0, int(self.cap - N))),
                    skip=N >= self.cap, i_var=0)

    def add(self, st, data, sel, centre_D, first_D, fs):
        from ..modes.pca import window_sums, window_start
        if st is None:
            return
        if st["group"] == "KV" and st["i_var"] < self.n_var:
            k = min(self.n_var - st["i_var"], len(data))
            d = data[:k].astype(np.float64) - self.off
            self.var_s += float(np.sum(d ** 2)); self.var_n += d.size; st["i_var"] += k
        if st["skip"] or st["budget"] <= 0:
            return
        ii = np.flatnonzero(sel)
        if np.isfinite(st["budget"]):
            ii = ii[: st["budget"]]
            st["budget"] -= len(ii)
        if not len(ii):
            return
        a, b, n = window_sums(data[ii], window_start(centre_D[ii], first_D[ii], fs, self.half), self.nb, self.B, self.off)
        S = self.S[st["group"]]
        S[0] += a; S[1] += b; S[2] += n

    def result(self):
        from ..modes.pca import pca_from_sums
        if self.var_n == 0:
            return dict(available=False, reason="kein K-V-Lauf", n_A=int(self.S["A"][2]), n_V=0)
        r = pca_from_sums(tuple(self.S["A"]), tuple(self.S["KV"]), self.var_s / self.var_n, self.spec, self.half_ns,
                          self.B)
        r["streaming"] = dict(incremental_covariance=True,
                              max_records=None if not np.isfinite(self.cap) else int(self.cap))
        return r


class _Mom:
    """Summen der Rohquadratur (verschoben um den ersten Blockmittelwert) und des LO-Monitors."""

    def __init__(self):
        self.n = 0; self.shift = None; self.S = np.zeros(5)
        self.lo_n = 0; self.lo_s = 0.0

    def add(self, q, lo):
        if len(q):
            if self.shift is None:
                self.shift = float(np.mean(q))
            y = q - self.shift
            p = np.ones_like(y)
            for k in range(5):
                self.S[k] += float(p.sum()); p = p * y
            self.n += len(q)
        f = np.isfinite(lo)
        self.lo_n += int(f.sum()); self.lo_s += float(lo[f].sum())

    @property
    def mean(self):
        return self.shift + self.S[1] / self.S[0]

    def central(self, about: float, k: int) -> float:
        """Σ (q − about)^k für k ≤ 4 (aus den verschobenen Summen)."""
        from math import comb
        a = self.shift - about
        return float(sum(comb(k, j) * self.S[j] * a ** (k - j) for j in range(k + 1)))

    @property
    def lo_mean(self):
        return self.lo_s / self.lo_n if self.lo_n else float("nan")


def _sync_slice(g, name, sl):
    return g[name][sl] if name in g else None


def _pass1(session, rep, h5s, ap, spec, bank, off, qf, chunk, pca=None):
    """Rohquadraturen je Lauf blockweise; schreibt nach ``qf`` (quadratures.h5), liefert Summen je Lauf."""
    dT = float(rep.get("delta", {}).get("delta_T_s", np.nan)) if rep.get("delta", {}).get("available") else np.nan
    if not np.isfinite(dT):
        raise ValueError("keine Δ-Kalibrierung in sync.json (hhd sync ohne --no-delay ausführen)")
    per_run_delta = rep.get("delta", {}).get("per_run", {})
    moms, info = {}, {}
    for r in session.runs:
        b = r.block_id
        if b not in h5s["runs"] or r.n_records == 0:
            r.release()
            continue
        g = h5s["runs"][b]
        dr = float(per_run_delta[b]["delta_s"]) if b in per_run_delta else np.nan
        herald_type = r.dataset_type in ("heralded", "herald_blocked")
        n = r.n_records
        out = qf.create_group(f"runs/{b}")
        out.attrs["dataset_type"] = r.dataset_type
        cols = dict(q_raw_lsb=np.float64, coverage=np.float32, clean=bool, seg_class=np.int8, t_session_s=np.float64,
                    lo_b_lsb=np.float64, centre_D_s=np.float64, main=bool, random=bool)
        ds = {k: out.create_dataset(k, (n,), dtype=t, chunks=(min(n, 1 << 15),)) for k, t in cols.items()}
        mom = _Mom()
        lo_b_all = _lo_b_iter(r, chunk)
        n_low = 0
        pst = pca.start_run({"heralded": "A", "vacuum": "KV"}.get(r.dataset_type)) if pca is not None else None
        for i0, hd in r.iter_headers(chunk):
            sl = slice(i0, i0 + len(hd["timestamp"]))
            tu = r.time_unit
            trig = hd["timestamp"].astype(np.float64) * tu
            first = (hd["timestamp"] + hd["record_start"]).astype(np.float64) * tu
            sc = g["seg_class"][sl]
            c = trig + dT
            clean = np.ones(len(trig), bool)
            sh = _sync_slice(g, "seg_herald", sl)
            if sh is not None and "herald_D_s" in g:
                m = sh >= 0
                if np.isfinite(dr) and m.any():
                    lo, hi = int(sh[m].min()), int(sh[m].max()) + 1
                    hD = g["herald_D_s"][lo:hi]
                    c[m] = hD[sh[m] - lo] + dr
                if herald_type:
                    clean = m & (g["seg_n_secondary"][sl] == 0) & ~g["seg_overlap_prev"][sl].astype(bool)
            data = r.read_records(sl)
            q, cov = project_records(data, (c - first) * r.fs, bank, off)
            lo_b = next(lo_b_all)
            clean &= cov >= 0.99
            n_low += int(np.sum(cov < 0.99))
            if herald_type:
                main = (sc == A.SEG_MATCHED) & clean
                rnd = (sc == A.SEG_RANDOM) & (cov >= 0.99)
            elif r.dataset_type == "random_trigger":
                main = (sc == A.SEG_RANDOM) & (cov >= 0.99); rnd = np.zeros(len(sc), bool)
            elif r.dataset_type == "state":
                main = (sc == A.SEG_STATE) & (cov >= 0.99); rnd = np.zeros(len(sc), bool)
            else:
                main = cov >= 0.99; rnd = np.zeros(len(sc), bool)
            if pst is not None:
                pca.add(pst, data, main if pst["group"] == "A" else np.ones(len(main), bool), c, first, r.fs)
            del data
            vals = dict(q_raw_lsb=q, coverage=cov, clean=clean, seg_class=sc, t_session_s=g["record_time_session_s"][sl],
                        lo_b_lsb=lo_b, centre_D_s=c, main=main, random=rnd)
            for k, v in vals.items():
                ds[k][sl] = v
            mom.add(q, lo_b)
        moms[b] = mom
        info[b] = dict(n_low_coverage=n_low)
        r.release()
    return moms, info


def _lo_b_iter(run, chunk):
    """LO-Monitor je Record blockweise (Kanal B; live mit ``markers/data_b`` interpoliert)."""
    if run.has_data_b:
        for _, blk in run.iter_records(chunk=chunk, channel="B"):
            yield blk.mean(axis=1)
        return
    if getattr(run, "has_marker_b", False):
        tm, mb = run.marker_b_means()          # O(Marker) – live, 1 kHz
        for _, hd in run.iter_headers(chunk):
            t = hd["timestamp"].astype(np.float64) * run.time_unit
            yield np.interp(t, tm, mb) if len(tm) > 1 else np.full(len(t), mb[0] if len(mb) else np.nan)
        return
    for _, hd in run.iter_headers(chunk):
        yield np.full(len(hd["timestamp"]), np.nan)


# ---------------------------------------------------------------- Kalibrierung aus Summen
def calibrate_from_moments(session, moms: dict, kinds: dict, p) -> tuple[Calibration, dict]:
    warn = []
    runs = {r.block_id: r for r in session.runs}
    kv = [b for b in moms if kinds[b] == "vacuum"]
    if not kv:
        raise ValueError("kein Vakuumlauf (K-V) in der Sitzung")
    N = sum(moms[b].n for b in kv)
    q0 = sum(moms[b].mean * moms[b].n for b in kv) / N
    var_kv = sum(moms[b].central(q0, 2) for b in kv) / N
    el = [b for b in moms if kinds[b] == "electronic"]
    n_el = 0
    if el:
        ne = sum(moms[b].n for b in el)
        me = sum(moms[b].mean * moms[b].n for b in el) / ne
        var_el, el_src, n_el = sum(moms[b].central(me, 2) for b in el) / ne, "Dunkellauf (LO aus): " + ", ".join(el), ne
    elif p.clearance_dB is not None:
        S = 10 ** (p.clearance_dB / 10)
        var_el, el_src = var_kv / (1 + S), f"Parameter clearance_dB = {p.clearance_dB}"
    else:
        var_el, el_src = None, "unbekannt"
        warn.append("elektronisches Rauschen unbekannt (kein Dunkellauf, kein clearance_dB): η_el fehlt im Budget")
    has_b = any(m.lo_n for m in moms.values())
    dark_b = p_ref = None; db_src = "–"; lo_rel = {}
    if has_b:
        eb = [b for b in el if moms[b].lo_n]
        if eb:
            dark_b = sum(moms[b].lo_s for b in eb) / sum(moms[b].lo_n for b in eb); db_src = "Dunkellauf (LO aus)"
        else:
            dark_b = float(session.device.get("lo_monitor_dark_lsb", 0.0))
            db_src = "Geräteattribut lo_monitor_dark_lsb (nominell)"
            warn.append("LO-Monitor: Dunkelwert nur nominell (im Labor mit LO aus messen)")
        p_ref = sum(moms[b].lo_s for b in kv) / max(sum(moms[b].lo_n for b in kv), 1) - dark_b
        for b, m in moms.items():
            if m.lo_n and kinds[b] != "electronic":
                lo_rel[b] = float((m.lo_mean - dark_b) / p_ref)
    elif p.norm.startswith("monitor"):
        raise ValueError("Normierung mit LO-Monitor verlangt Kanal B")
    kv_runs = sorted([dict(block=b, t=float(runs[b].t_start_s + runs[b].duration_s / 2),
                           var=float(moms[b].central(q0, 2) / moms[b].n), n=int(moms[b].n), lo_rel=lo_rel.get(b))
                      for b in kv], key=lambda d: d["t"])
    cal = Calibration(p.norm, float(q0), float(var_kv), int(N), kv_runs, var_el, el_src, dark_b, db_src, p_ref, lo_rel,
                      warnings=warn, n_el=n_el)
    for b, m in moms.items():
        if kinds[b] != "electronic":
            r = runs[b]
            lr = None if (dark_b is None or p_ref is None or not m.lo_n) else np.array([(m.lo_mean - dark_b) / p_ref])
            cal.scales[b] = float(np.mean(np.atleast_1d(cal.var_vac_at(r.t_start_s + r.duration_s / 2, b, lr))))
    drift = drift_vacuum_lo(cal, p)
    cal.warnings += drift["flags"]
    return cal, drift


# ---------------------------------------------------------------- Durchgang 2: Histogramme
def _pass2(qf, cal, kinds, chunk):
    """Normierte Quadraturen blockweise → Histogramme je (Gruppe, Lauf); schreibt ``x`` in quadratures.h5."""
    H: dict[str, dict[str, QHist]] = {}
    for b, g in qf["runs"].items():
        if kinds[b] == "electronic":
            continue
        n = g["q_raw_lsb"].shape[0]
        xd = g.create_dataset("x", (n,), dtype=np.float64, chunks=(min(n, 1 << 15),))
        key = KEY.get(kinds[b], kinds[b])
        hm, hr, hkh = QHist(), QHist(), QHist()
        for i0 in range(0, n, chunk):
            sl = slice(i0, i0 + chunk)
            q, t, lo = g["q_raw_lsb"][sl], g["t_session_s"][sl], g["lo_b_lsb"][sl].astype(np.float64)
            x = cal.normalize(q, t, b, lo)
            xd[sl] = x
            hm.add(x[g["main"][sl]])
            hr.add(x[g["random"][sl]])
            if kinds[b] == "herald_blocked":
                hkh.add(x[g["seg_class"][sl] == A.SEG_MATCHED])
        H.setdefault(key, {})[b] = hm
        if hr.n:
            H.setdefault("B_interleaved", {})[b] = hr
        if hkh.n:
            H.setdefault("_KH_matched", {})[b] = hkh
    return H


# ---------------------------------------------------------------- Bootstrap
def _vac_moments(cal, moms, kinds):
    kv = [b for b in moms if kinds[b] == "vacuum"]
    N = sum(moms[b].n for b in kv)
    c4 = sum(moms[b].central(cal.q0, 4) for b in kv) / N
    return N, c4


def bootstrap_hist(H, keys, point, cal, vac, ap, rng):
    N, c4 = vac
    res = {k: {q: [] for q in ("var_x", "W00", "W00_moments", "eta_fit", "eta_var", "mean_n", "p1", "mandel_Q", "g2")} for k in keys}
    res["diff"] = {q: [] for q in ("var_x", "W00", "W00_moments", "eta_fit")}
    sd_q0 = np.sqrt(cal.var_kv / N); sd_v = np.sqrt(max(c4 - cal.var_kv ** 2, 0.0) / N)
    k0 = np.sqrt(VAC / cal.var_kv)
    for _ in range(ap.n_boot):
        if ap.boot_vacuum:
            q0s = cal.q0 + rng.normal(0, sd_q0); vs = cal.var_kv + rng.normal(0, sd_v)
            c = float(np.sqrt(cal.var_kv / vs)); d = float((cal.q0 - q0s) * k0)
        else:
            c, d = 1.0, 0.0
        st = {}
        for k in keys:
            parts = [(rng.poisson(h.h), c, d) for h in H[k].values()]
            g = stats_from_hist(parts, ap.nmax, max(ap.tol, 1e-7), p0=point[k]["_p"], full=False)
            st[k] = g
            for q in ("var_x", "W00", "W00_moments", "eta_fit", "eta_var", "mean_n"):
                res[k][q].append(g[q])
            res[k]["p1"].append(g["p_n"][1])
            ps_ = photon_stats(np.asarray(g["p_n"], float))
            res[k]["mandel_Q"].append(ps_["mandel_Q"]); res[k]["g2"].append(ps_["g2"])
        if "A" in st and "B" in st:
            for q in res["diff"]:
                res["diff"][q].append(st["A"][q] - st["B"][q])
    return res


# ---------------------------------------------------------------- Vergleich, Nulltests aus Histogrammen
def _ks_hist(h1: np.ndarray, h2: np.ndarray) -> tuple[float, float]:
    from scipy.stats import kstwobign
    n1, n2 = h1.sum(), h2.sum()
    D = float(np.max(np.abs(np.cumsum(h1) / n1 - np.cumsum(h2) / n2)))
    en = np.sqrt(n1 * n2 / (n1 + n2))
    return D, float(kstwobign.sf((en + 0.12 + 0.11 / en) * D))


def compare_hist(HA: QHist, HB: QHist, gA, gB, boot, ap) -> dict:
    edges = np.linspace(-ap.hist_lim, ap.hist_lim, ap.hist_bins + 1)
    out = dict()
    step = int(round((edges[1] - edges[0]) * FINE))
    i0 = int(round((-ap.hist_lim + LIM) * FINE))
    aligned = abs(step / FINE - (edges[1] - edges[0])) < 1e-12 and abs(i0 / FINE - (-ap.hist_lim + LIM)) < 1e-12
    if not aligned:
        raise ValueError("hist_lim/hist_bins passen nicht auf das Feinraster 1/120")
    sl = slice(i0, i0 + step * ap.hist_bins)
    hA = HA.h[sl].reshape(-1, step).sum(1); hB = HB.h[sl].reshape(-1, step).sum(1)
    w = np.diff(edges)
    dA, dB = hA / (HA.n * w), hB / (HB.n * w)
    eA, eB = np.sqrt(hA) / (HA.n * w), np.sqrt(hB) / (HB.n * w)
    m = (hA + hB) > 0
    chi2 = float(np.sum((dA - dB)[m] ** 2 / (eA ** 2 + eB ** 2)[m]))
    D, pv = _ks_hist(HA.h, HB.h)
    out.update(histogram=dict(edges=edges.tolist(), A=dA.tolist(), B=dB.tolist(), A_err=eA.tolist(), B_err=eB.tolist(),
                              chi2=chi2, ndof=int(m.sum())),
               ks=dict(statistic=D, pvalue=pv, method="Histogramm-CDF (1/120), asymptotisch"),
               wigner_r=gA["wigner_r"], wigner_diff=(np.array(gA["wigner_radial"]) - np.array(gB["wigner_radial"])).tolist())
    for q in ("var_x", "W00", "W00_moments", "eta_fit"):
        d = gA[q] - gB[q]
        e = float(np.std(boot["diff"][q], ddof=1)) if boot and boot["diff"][q] else float("nan")
        out[f"diff_{q}"] = dict(value=float(d), err=e, z=float(d / e) if e > 0 else float("nan"))
    out["eta_from_comparison"] = dict(value=out["diff_var_x"]["value"], err=out["diff_var_x"]["err"],
                                      model="Var_A − Var_B (verlustbehaftetes |1⟩, B ≈ Vakuum + n_B)")
    return out


def null_tests_hist(H) -> dict:
    out = {}
    for key in ("A", "B"):
        G = list(H.get(key, {}).items())
        if len(G) < 2:
            continue
        h1 = merge_all(h for _, h in G[0::2]); h2 = merge_all(h for _, h in G[1::2])
        m1, m2 = h1.moments(), h2.moments()
        _, pv = _ks_hist(h1.h, h2.h)
        out[key + key] = dict(runs_1=[b for b, _ in G[0::2]], runs_2=[b for b, _ in G[1::2]], n1=h1.n, n2=h2.n, ks_p=pv,
                              var_diff=m1["var_x"] - m2["var_x"],
                              var_z=float((m1["var_x"] - m2["var_x"]) / np.hypot(m1["var_err"], m2["var_err"])))
    return out


# ---------------------------------------------------------------- Kette
def analyze_session_streaming(path, ap: AnalysisParams | None = None, out_dir=None, sync_params=None,
                              write: bool = True, chunk: int = 1024) -> dict:
    from ..io.session import load_session
    from .syncdata import ensure_sync
    ap = ap or AnalysisParams()
    t0 = time.time(); timing = {}
    session = load_session(path, lazy=True)
    sync_dir = Path(session.path) / "sync"
    if not (sync_dir / "sync.h5").exists() or not (sync_dir / "sync.json").exists():
        from ..sync.pipeline import sync_session
        sync_session(session, sync_params, sync_dir)
    rep = json.loads((sync_dir / "sync.json").read_text(encoding="utf-8"))
    timing["load_sync"] = time.time() - t0
    warnings: list[str] = []
    if ap.mode_source != "parametric":
        warnings.append(f"Datenstrom-Auswertung: mode_source {ap.mode_source!r} nicht unterstützt → parametrisch")
    spec = ModeSpec(resolve_mode_shape(ap.mode_shape, session.device), ap.gamma, session.device["sampling_rate_Hz"],
                    session.device.get("analog_bandwidth_Hz") if ap.bandwidth_model else None)
    bank = template_bank(spec)
    kv_runs = session.runs_of("vacuum")
    off = adc_offset(kv_runs)
    for r in kv_runs:
        r.release()
    kinds = {r.block_id: r.dataset_type for r in session.runs}
    od = Path(out_dir) if out_dir else Path(session.path) / "analysis"
    od.mkdir(parents=True, exist_ok=True)
    qpath = od / "quadratures.h5"
    t = time.time()
    with h5py.File(sync_dir / "sync.h5", "r") as h5s, h5py.File(qpath, "w") as qf:
        qf.attrs["schema"] = "hhd-quadratures/1.0"
        qf.attrs["streaming"] = True
        pacc = _PCAAcc(spec, off, ap.pca_max_records) if ap.pca else None
        moms, info = _pass1(session, rep, h5s, ap, spec, bank, off, qf, chunk, pacc)
        timing["extract"] = time.time() - t
        kinds = {b: kinds[b] for b in moms}
        t = time.time()
        cal, drift_kv = calibrate_from_moments(session, moms, kinds, ap.calib_params())
        qf.attrs["calibration_json"] = json.dumps(_clean(cal.to_dict()), ensure_ascii=False)
        timing["calibrate"] = time.time() - t
        t = time.time()
        H = _pass2(qf, cal, kinds, chunk)
        for k, hs in list(H.items()) + [("EL", {b: None for b in moms if kinds[b] == "electronic"})]:
            if k.startswith("_"):
                continue
            for b in hs:                                  # Gruppenmasken wie in der In-Memory-Auswertung
                src = qf[f"runs/{b}/{'random' if k == 'B_interleaved' else 'main'}"]
                dst = qf.create_dataset(f"groups/{k}/{b}", src.shape, dtype=bool)
                for i0 in range(0, src.shape[0], 1 << 16):
                    dst[i0:i0 + (1 << 16)] = src[i0:i0 + (1 << 16)]
        n_el = {b: int(sum(int(qf[f"groups/EL/{b}"][i0:i0 + (1 << 16)].sum())
                           for i0 in range(0, qf[f"groups/EL/{b}"].shape[0], 1 << 16)))
                for b in moms if kinds[b] == "electronic"}
        timing["histogram"] = time.time() - t
        kh_runs = [b for b in moms if kinds[b] == "herald_blocked"]
        pseudo = {b: SimpleNamespace(dataset_type=kinds[b], seg_class=qf[f"runs/{b}/seg_class"][:]) for b in kh_runs}
        pseudo.update({b: SimpleNamespace(dataset_type="vacuum", seg_class=None) for b in moms if kinds[b] == "vacuum"})
    n_out = sum(h.n_out for hs in H.values() for h in hs.values())
    if n_out:
        warnings.append(f"Datenstrom-Auswertung: {n_out} Quadraturen außerhalb ±{LIM:g} (am Rand einsortiert)")
    xt = dict(available=False, reason="kein K-H-Lauf")
    if kh_runs and "_KH_matched" in H:
        km = merge_all(H["_KH_matched"].values()).moments()
        xt = crosstalk_check(session, pseudo, cal, ap.calib_params(), kh_stats=dict(n=km["n"], mean=km["mean_x"],
                                                                                   var=km["var_x"]))
        for r in session.runs:
            r.release()
    warnings += cal.warnings + xt.get("flags", [])
    pca = None
    if pacc is not None and "A" in H and any(kinds[b] == "vacuum" for b in moms):
        t = time.time()
        pca = pacc.result()
        timing["pca"] = time.time() - t
        if not pca["available"]:
            warnings.append("PCA nicht möglich: " + pca["reason"])
            pca = None
    groups = {k: dict(runs=list(v), n=int(sum(h.n for h in v.values()))) for k, v in H.items() if not k.startswith("_")}
    if n_el:
        groups["EL"] = dict(runs=list(n_el), n=int(sum(n_el.values())))
    result: dict = dict(schema=SCHEMA, software=dict(name="hhd", version=__version__), session=str(session.path),
                        session_id=session.session_id, campaign_id=session.campaign_id,
                        calibration_id=session.calibration_id, params=asdict(ap),
                        mode=dict(spec.to_dict(), bandwidth_effect=float(bank.bandwidth_effect), adc_offset_lsb=off),
                        laser=laser_info(session.device),
                        calibration=cal.to_dict(), drift_vacuum_lo=drift_kv, crosstalk=xt, pca=pca, groups=groups,
                        streaming=dict(chunk_records=chunk, hist_lim=LIM, hist_bin=1 / FINE, maxlik_bin=REBIN / FINE,
                                       bootstrap="Poisson je Bin und Lauf, Vakuum aus K-V-Momenten",
                                       n_outside=n_out))
    rng = np.random.default_rng(ap.seed)
    t = time.time()
    keys = [k for k in ("A", "B", "B_interleaved", "KH", "state") if k in H and groups[k]["n"] > 10]
    HG = {k: merge_all(H[k].values()) for k in keys}
    point = {k: stats_from_hist([(HG[k].h, 1.0, 0.0)], ap.nmax, ap.tol, exact=HG[k].moments()) for k in keys}
    timing["reconstruct"] = time.time() - t
    t = time.time()
    bkeys = [k for k in ("A", "B", "state") if k in point]
    boot = bootstrap_hist(H, bkeys, point, cal, _vac_moments(cal, moms, kinds), ap, rng) if ap.n_boot > 1 else None
    timing["bootstrap"] = time.time() - t
    res_groups = {}
    from ..recon.intervals import intervals
    for k, g in point.items():
        gg = {q: v for q, v in g.items() if q != "_p"}
        if boot and k in boot:
            for q in ("var_x", "W00", "W00_moments", "eta_fit", "eta_var", "mean_n", "p1"):
                pv = g[q] if q not in ("mean_n", "p1") else (g["mean_n"] if q == "mean_n" else g["p_n"][1])
                s = _summ(boot[k][q], pv)
                gg[f"{q}_err"] = s["err"]; gg[f"{q}_boot"] = s
            for q in ("mandel_Q", "g2"):                   # Mandel Q / g²(0) aus p_n je Ziehung (nan bei ⟨n⟩ ≈ 0)
                v = np.asarray(boot[k].get(q, []), float); v = v[np.isfinite(v)]
                gg[f"{q}_err"] = float(np.std(v, ddof=1)) if len(v) > 2 else float("nan")
                gg[f"{q}_n_boot"] = int(len(v))
            cis = {}
            for q in CI_QUANT:
                ci = intervals(g[q], boot[k][q])
                if not ci:
                    continue
                cis[q] = {m: v for m, v in ci.items() if not m.startswith("_")}
                meth = "normal" if ap.ci_method == "auto" else ap.ci_method
                meth = meth if meth in cis[q] else "percentile"
                gg[f"{q}_ci68"], gg[f"{q}_ci95"] = cis[q][meth]["68"], cis[q][meth]["95"]
                gg[f"{q}_ci_method"] = meth
                gg[f"{q}_ci_params"] = ci["_params"]
            gg["ci_all"] = cis
            if ap.bias_correct:
                gg["W00_bc"] = g["W00"] - gg["W00_boot"]["bias_boot"]
        res_groups[k] = gg
    result["reconstruction"] = res_groups
    runs = {r.block_id: r for r in session.runs}
    per = []
    for r in session.runs:
        for key in ("A", "B"):
            h = H.get(key, {}).get(r.block_id)
            if h is not None and h.n > 1:
                m = h.moments()
                per.append(dict(block=r.block_id, group=key, t=float(r.t_start_s + r.duration_s / 2), n=m["n"],
                                var_x=m["var_x"], var_err=m["var_err"], W00_moments=m["W00_moments"],
                                W00_moments_err=m["W00_moments_err"]))
    result["drift_AB"] = drift_ab_from_per(per, ap)
    warnings += result["drift_AB"]["flags"]
    comp = None
    if "A" in point and "B" in point:
        t = time.time()
        comp = compare_hist(HG["A"], HG["B"], point["A"], point["B"], boot, ap)
        comp["null_tests"] = null_tests_hist(H)
        for nk, nt in comp["null_tests"].items():
            if abs(nt["var_z"]) > ap.drift_z or nt["ks_p"] < ap.drift_p:
                warnings.append(f"Nulltest {nk}: Lauf-Hälften unterscheiden sich (Var z = {nt['var_z']:+.1f}, "
                                f"KS p = {nt['ks_p']:.1e})")
        result["comparison"] = comp
        timing["compare"] = time.time() - t
    sd = SimpleNamespace(runs={b: SimpleNamespace(report=rep["runs"].get(b, {})) for b in runs})
    result["efficiency_budget"] = efficiency_budget(session, sd, cal, res_groups, pca, comp, ap)
    result["state"] = session_state(session)
    if ap.phase:
        t = time.time()
        from .phase import FileSource, annotate_warnings, phase_analysis
        result["phase"] = phase_analysis(session, FileSource(qpath), cal, ap,
                                         (result.get("efficiency_budget") or {}).get("xi"), result["state"], od)
        warnings += result["phase"].get("warnings", [])
        warnings[:] = annotate_warnings(warnings, result["phase"], result["state"])
        timing["phase"] = time.time() - t
    result["warnings"] = warnings
    timing["total"] = time.time() - t0
    result["performance"] = dict(timing_s=timing, n_records=int(sum(m.n for m in moms.values())))
    if write:
        (od / "result.json").write_text(json.dumps(_clean(result), indent=1, ensure_ascii=False), encoding="utf-8")
        from .report import write_report
        write_report(result, od / "report.md")
        result["_out_dir"] = str(od)
    return result
