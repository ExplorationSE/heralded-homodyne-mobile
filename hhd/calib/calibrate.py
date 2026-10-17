"""Vakuumkalibrierung und Normierung der Quadraturen (Gesamtplan v1.7, AP Normierung/Kalibrierung).

Modell der Rohquadratur (LSB) eines Records zur Zeit t mit LO-Leistung P (relativ):

    q = s_v·√(P/P_v)·x_opt + e,   Var(e) = σ_el² (unabhängig von P)

K-V (Vakuum, LO an): Var(q_KV) = s² ≡ s_v²·½ + σ_el². Normiert wird stets mit der Vakuumvarianz der K-V-Läufe
(x = q/s·√½), sodass elektronisches Rauschen wie ein Verlust η_el = 1 − σ_el²/Var(q_KV) wirkt
(= S/(1+S) mit der Clearance S = s_v²·½/σ_el²).

Normierungen (``norm``):

* ``pooled``        gemeinsame Kalibrierung aller K-V-Läufe der Sitzung (Standard);
* ``interpolated``  Vakuumvarianz zeitlich linear zwischen den K-V-Läufen (konstant außerhalb);
* ``monitor``       gemeinsame Kalibrierung, je Lauf mit dem LO-Monitor skaliert:
                    Var_v(Lauf) = Var_KV·[(1−f_el)·P_Lauf/P_KV + f_el], f_el = σ_el²/Var_KV;
* ``monitor_record`` wie ``monitor``, aber je Record mit dem Kanal-B-Mittel dieses Records.

Elektronisches Rauschen: aus Läufen mit LO aus (``electronic``), sonst aus dem Parameter ``clearance_dB``
(Labormessung), sonst unbekannt (Warnung; η_el fehlt im Budget).
LO-Monitor: Dunkelwert aus ``electronic``-Läufen (Kanal B), sonst aus dem Geräteattribut
``lo_monitor_dark_lsb`` (Warnung: im Labor mit LO aus messen).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np

VAC = 0.5
NORMS = ("pooled", "interpolated", "monitor", "monitor_record")


@dataclass
class CalibParams:
    norm: str = "pooled"
    clearance_dB: float | None = None
    drift_z: float = 3.0
    lo_rel_warn: float = 0.01
    crosstalk_z: float = 5.0
    crosstalk_bin: int = 10                  # Samples je Bin für die Wellenformprüfung (4 ns)


@dataclass
class Calibration:
    norm: str
    q0: float                                # Mittel der Vakuumquadratur (LSB, Restoffset)
    var_kv: float                            # gepoolte Vakuumvarianz (LSB²)
    n_kv: int
    kv_runs: list                            # je K-V-Lauf: block, t, var, n, lo_rel
    var_el: float | None                     # elektronisches Rauschen (LSB²) oder None
    el_source: str
    dark_b: float | None
    dark_b_source: str
    p_ref: float | None                      # mittleres Kanal-B-Signal der K-V-Records (LSB über Dunkel)
    lo_rel: dict = field(default_factory=dict)            # Lauf → P/P_KV
    scales: dict = field(default_factory=dict)            # Lauf → Var_v (LSB²) der verwendeten Normierung
    warnings: list = field(default_factory=list)
    vac_q: np.ndarray | None = None                       # Rohquadraturen K-V (für gemeinsamen Bootstrap)
    vac_t: np.ndarray | None = None
    n_el: int = 0                                         # Records des Dunkellaufs (0 = Parameter/unbekannt)

    @property
    def f_el(self) -> float:
        return float(self.var_el / self.var_kv) if self.var_el is not None else 0.0

    @property
    def clearance(self) -> float:
        return float((self.var_kv - self.var_el) / self.var_el) if self.var_el else float("nan")

    @property
    def eta_el_err(self) -> float | None:
        """Statistischer Fehler von η_el (nur aus Dunkellauf): f_el·√(2/N_el + 2/N_KV)."""
        if self.var_el is None or not self.n_el:
            return None
        return float(self.f_el * np.sqrt(2 / self.n_el + 2 / self.n_kv))

    @property
    def eta_el(self) -> float:
        return float(1 - self.var_el / self.var_kv) if self.var_el is not None else float("nan")

    def var_vac_at(self, t: float | np.ndarray, block: str | None = None, lo_rec: np.ndarray | None = None,
                   var_kv: float | None = None, kv_vars: np.ndarray | None = None) -> np.ndarray | float:
        """Vakuumvarianz (LSB²) für einen Lauf/Records gemäß Normierung; ``var_kv``/``kv_vars`` erlauben
        den Bootstrap mit neu gezogenen Vakuumdaten."""
        vkv = self.var_kv if var_kv is None else var_kv
        if self.norm == "pooled":
            return vkv
        if self.norm == "interpolated":
            tk = np.array([r["t"] for r in self.kv_runs])
            vk = np.array([r["var"] for r in self.kv_runs]) if kv_vars is None else kv_vars
            return np.interp(t, tk, vk)
        fe = self.f_el
        if self.norm == "monitor":
            P = self.lo_rel.get(block, np.nan)
            return vkv * ((1 - fe) * P + fe)
        if self.norm == "monitor_record":
            return vkv * ((1 - fe) * lo_rec + fe)
        raise ValueError(self.norm)

    def normalize(self, q: np.ndarray, t: np.ndarray, block: str, lo_rec: np.ndarray | None = None,
                  **kw) -> np.ndarray:
        v = self.var_vac_at(t, block, self.lo_rel_record(lo_rec), **kw)
        return (q - self.q0) * np.sqrt(VAC / v)

    def lo_rel_record(self, lo_b: np.ndarray | None) -> np.ndarray | None:
        if lo_b is None or self.p_ref is None or self.dark_b is None:
            return None
        return (lo_b - self.dark_b) / self.p_ref

    def to_dict(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if k not in ("vac_q", "vac_t")}
        d.update(clearance=self.clearance if self.var_el else None, clearance_dB=float(10 * np.log10(self.clearance)) if self.var_el else None,
                 eta_el=self.eta_el if self.var_el is not None else None, eta_el_err=self.eta_el_err, f_el=self.f_el, scale_lsb=float(np.sqrt(self.var_kv / VAC)))
        return d


def calibrate(session, quads: dict, params: CalibParams | None = None) -> tuple[Calibration, dict]:
    """``quads``: Block → RunQuadratures (Rohquadraturen). Rückgabe (Kalibrierung, Driftindikatoren)."""
    p = params or CalibParams()
    if p.norm not in NORMS:
        raise ValueError(f"Normierung {p.norm!r}: {', '.join(NORMS)}")
    warn = []
    runs = {r.block_id: r for r in session.runs}
    kv = [b for b, rq in quads.items() if rq.dataset_type == "vacuum"]
    if not kv:
        raise ValueError("kein Vakuumlauf (K-V) in der Sitzung")
    vq = np.concatenate([quads[b].q for b in kv])
    vt = np.concatenate([quads[b].t_session for b in kv])
    q0 = float(np.mean(vq))
    var_kv = float(np.mean((vq - q0) ** 2))
    # elektronisches Rauschen
    el = [b for b, rq in quads.items() if rq.dataset_type == "electronic"]
    n_el = 0
    if el:
        qe = np.concatenate([quads[b].q for b in el])
        var_el, el_src = float(np.var(qe)), "Dunkellauf (LO aus): " + ", ".join(el)
        n_el = int(len(qe))
    elif p.clearance_dB is not None:
        S = 10 ** (p.clearance_dB / 10)
        var_el, el_src = var_kv / (1 + S), f"Parameter clearance_dB = {p.clearance_dB}"
    else:
        var_el, el_src = None, "unbekannt"
        warn.append("elektronisches Rauschen unbekannt (kein Dunkellauf, kein clearance_dB): η_el fehlt im Budget")
    # LO-Monitor
    has_b = any(np.isfinite(rq.lo_b).any() for rq in quads.values())
    dark_b = p_ref = None; db_src = "–"
    lo_rel = {}
    if has_b:
        eb = [quads[b].lo_b for b in el if np.isfinite(quads[b].lo_b).any()]
        if eb:
            dark_b, db_src = float(np.mean(np.concatenate(eb))), "Dunkellauf (LO aus)"
        else:
            dark_b = float(session.device.get("lo_monitor_dark_lsb", 0.0))
            db_src = "Geräteattribut lo_monitor_dark_lsb (nominell)"
            warn.append("LO-Monitor: Dunkelwert nur nominell (im Labor mit LO aus messen)")
        kvb = np.concatenate([quads[b].lo_b for b in kv])
        p_ref = float(np.nanmean(kvb) - dark_b)
        for b, rq in quads.items():
            if np.isfinite(rq.lo_b).any() and rq.dataset_type != "electronic":
                lo_rel[b] = float((np.nanmean(rq.lo_b) - dark_b) / p_ref)
    elif p.norm.startswith("monitor"):
        raise ValueError("Normierung mit LO-Monitor verlangt Kanal B")
    kv_runs = []
    for b in kv:
        rq = quads[b]
        r = runs[b]
        kv_runs.append(dict(block=b, t=float(r.t_start_s + r.duration_s / 2), var=float(np.mean((rq.q - q0) ** 2)),
                            n=int(len(rq.q)), lo_rel=lo_rel.get(b)))
    kv_runs.sort(key=lambda d: d["t"])
    cal = Calibration(p.norm, q0, var_kv, int(len(vq)), kv_runs, var_el, el_src, dark_b, db_src, p_ref, lo_rel,
                      warnings=warn, vac_q=vq - q0, vac_t=vt, n_el=n_el)
    for b, r in runs.items():
        if b in quads and quads[b].dataset_type != "electronic":
            cal.scales[b] = float(np.mean(np.atleast_1d(cal.var_vac_at(r.t_start_s + r.duration_s / 2, b,
                                                                         cal.lo_rel_record(quads[b].lo_b)))))
    drift = drift_vacuum_lo(cal, p)
    cal.warnings += drift["flags"]
    return cal, drift


def drift_vacuum_lo(cal: Calibration, p: CalibParams) -> dict:
    """K-V-Drift (erster ↔ letzter K-V-Lauf, Trend) und LO-Änderung laut Kanal B."""
    out: dict = dict(flags=[])
    kv = cal.kv_runs
    if len(kv) >= 2:
        v0, v1 = kv[0], kv[-1]
        rel = v1["var"] / v0["var"] - 1
        err = float(np.sqrt(2 / v0["n"] + 2 / v1["n"]))
        t = np.array([r["t"] for r in kv]); v = np.array([r["var"] for r in kv]); n = np.array([r["n"] for r in kv])
        sv = v * np.sqrt(2 / n)
        w = 1 / sv ** 2
        A = np.vstack([np.ones_like(t), t - t.mean()]).T
        cov = np.linalg.inv(A.T @ (A * w[:, None]))
        c = cov @ (A.T @ (w * v))
        chi2 = float(np.sum(w * (v - A @ c) ** 2))
        out["vacuum"] = dict(first=v0["block"], last=v1["block"], var_rel_change=float(rel), err=err, z=float(rel / err),
                             slope_rel_per_s=float(c[1] / c[0]), slope_err=float(np.sqrt(cov[1, 1]) / c[0]),
                             chi2=chi2, ndof=len(kv) - 2,
                             per_run={r["block"]: r["var"] / cal.var_kv - 1 for r in kv})
        if abs(rel / err) > p.drift_z:
            out["flags"].append(f"K-V: Vakuumvarianz ändert sich um {rel:+.2%} (z = {rel / err:+.1f})")
    if cal.lo_rel:
        vals = cal.lo_rel
        dev = {b: v - 1 for b, v in vals.items()}
        worst = max(dev, key=lambda b: abs(dev[b]))
        out["lo_monitor"] = dict(per_run=vals, max_rel_dev=float(dev[worst]), worst=worst)
        if abs(dev[worst]) > p.lo_rel_warn:
            out["flags"].append(f"LO-Monitor: Lauf {worst} weicht um {dev[worst]:+.2%} von K-V ab")
    return out


def crosstalk_check(session, quads: dict, cal: Calibration, p: CalibParams | None = None,
                    max_records: int = 20000) -> dict:
    """K-H-Übersprechprüfung: Records mit Herald-Trigger bei blockiertem Signal (K-H) gegen K-V.

    1. Modenquadratur: ⟨x⟩ ≈ 0 und Var x ≈ ½ (z-Werte);
    2. Wellenform: mittlerer K-H-Record minus mittlerer K-V-Record (Bins zu ``crosstalk_bin`` Samples),
       max |z| über alle Bins; ein Trigger-Übersprechen erscheint als deterministische Spur.
    """
    p = p or CalibParams()
    kh = [b for b, rq in quads.items() if rq.dataset_type == "herald_blocked"]
    kv = [b for b, rq in quads.items() if rq.dataset_type == "vacuum"]
    if not kh:
        return dict(available=False, reason="kein K-H-Lauf")
    runs = {r.block_id: r for r in session.runs}
    xs = []
    for b in kh:
        rq = quads[b]
        r = runs[b]
        m = rq.seg_class == 0
        xs.append(cal.normalize(rq.q[m], rq.t_session[m], b, rq.lo_b[m]))
    x = np.concatenate(xs)
    n = len(x)
    mean, var = float(np.mean(x)), float(np.var(x))
    z_mean = mean / np.sqrt(var / n)
    z_var = (var - VAC) / (VAC * np.sqrt(2 / n + 2 / cal.n_kv))      # inkl. Unsicherheit der Vakuumnormierung

    B = p.crosstalk_bin

    def avg_wave(blocks, only_matched):
        """Mittel und Varianz der Bin-Mittel je Record (Samples innerhalb eines Bins sind korreliert)."""
        s = None; s2 = None; cnt = 0
        for b in blocks:
            r = runs[b]
            sel = (quads[b].seg_class == 0) if only_matched else np.ones(r.n_records, bool)
            for i0, blk in r.iter_records(chunk=1024):
                y = blk[sel[i0:i0 + len(blk)]].astype(np.float64)
                L = (y.shape[1] // B) * B
                y = y[:, :L].reshape(len(y), -1, B).mean(-1)
                if s is None:
                    s = np.zeros(y.shape[1]); s2 = np.zeros(y.shape[1])
                s += y.sum(0); s2 += (y ** 2).sum(0); cnt += len(y)
                if cnt >= max_records:
                    break
        return s / cnt, s2 / cnt - (s / cnt) ** 2, cnt

    mh, vh, nh = avg_wave(kh, True)
    mv, vv, nv = avg_wave(kv, False)
    d = mh - mv
    se = np.sqrt(vh / nh + vv / nv)
    z = d / se
    j = int(np.argmax(np.abs(z)))
    pre = -float(session.device.get("horizontal_offset", 0))
    out = dict(available=True, runs=kh, n=n, mean_x=mean, var_x=var, z_mean=float(z_mean), z_var=float(z_var),
               waveform=dict(n_kh=nh, n_kv=nv, bin_samples=B, max_abs_z=float(abs(z[j])),
                             at_ns_after_trigger=float((j * B + B / 2 - pre) / session.device["sampling_rate_Hz"] * 1e9),
                             max_amp_lsb=float(d[j]), n_bins=int(len(z))), flags=[])
    if abs(z_mean) > p.drift_z or abs(z_var) > p.drift_z:
        out["flags"].append(f"K-H: Modenquadratur weicht von Vakuum ab (z_mean = {z_mean:+.1f}, z_var = {z_var:+.1f})")
    if abs(z[j]) > p.crosstalk_z:
        out["flags"].append(f"K-H: Trigger-Übersprechen in der Wellenform ({abs(z[j]):.1f}σ bei "
                            f"{out['waveform']['at_ns_after_trigger']:.0f} ns nach dem Trigger)")
    return out
