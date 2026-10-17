"""Auswertekette einer Sitzung: sync.h5 → Quadraturen → Kalibrierung → Rekonstruktion → Vergleich A/B → Bericht.

Benutzt ausschließlich die Sitzungsdaten (``session.json``, ``digitizer.h5``, PTU) und ``sync/``; die Wahrheitsdatei
wird nie gelesen (Abgleich nur in ``hhd.sim.analysis_validate``).

Ausgabe in ``<sitzung>/analysis/``: ``result.json`` (Schema ``hhd-result/1.0``), ``quadratures.h5`` (normierte
Quadraturen je Lauf mit Masken), ``report.md``.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import h5py
import numpy as np

from .. import __version__
from ..calib.calibrate import CalibParams, Calibration, calibrate, crosstalk_check
from ..modes.extract import ModeSpec, adc_offset, extract_run, template_bank
from ..modes.pca import pca_fitted_template, pca_mode, pca_template
from ..recon.stats import group_stats
from .syncdata import ensure_sync, group_mask, mode_centres

SCHEMA = "hhd-result/1.0"


@dataclass
class AnalysisParams:
    mode_shape: str = "double_exponential"
    gamma: float = 1 / 40e-9
    bandwidth_model: bool = True            # Analogbandbreite des Digitizers in der Vorlage berücksichtigen
    mode_source: str = "parametric"         # "pca": γ/Versatz aus PCA; "pca_raw": rohe Eigenmode (Prüfung)
    pca: bool = True
    norm: str = "pooled"
    clearance_dB: float | None = None
    estimator: str = "maxlik"
    nmax: int = 10
    tol: float = 1e-8
    binned_above: int = 50000               # ab dieser Gruppengröße Histogramm-MaxLik (gleiches Ergebnis, ~100× schneller)
    n_boot: int = 200
    boot_vacuum: bool = True                # OP-1: Vakuumnormierung im Bootstrap neu ziehen
    bias_correct: bool = True               # OP-2: Bootstrap-Biaskorrektur für W(0,0)
    seed: int = 1
    dark_count_rate: float = 100.0          # Herald-Dunkelzählrate (1/s) für ξ im Effizienzbudget
    drift_z: float = 3.0
    hist_bins: int = 60
    hist_lim: float = 5.0

    def calib_params(self) -> CalibParams:
        return CalibParams(norm=self.norm, clearance_dB=self.clearance_dB, drift_z=self.drift_z)


# ---------------------------------------------------------------- Quadraturen
def extract_session(session, sd, ap: AnalysisParams, spec: ModeSpec | None = None):
    spec = spec or ModeSpec(ap.mode_shape, ap.gamma, session.device["sampling_rate_Hz"],
                            session.device.get("analog_bandwidth_Hz") if ap.bandwidth_model else None)
    bank = template_bank(spec)
    kv = session.runs_of("vacuum")
    off = adc_offset(kv)
    quads, centres = {}, {}
    for r in session.runs:
        if r.block_id not in sd.runs or r.n_records == 0:
            continue
        rsd = sd.runs[r.block_id]
        c, clean = mode_centres(r, rsd, sd)
        centres[r.block_id] = c
        quads[r.block_id] = extract_run(r, c, bank, off, rsd.seg_class, clean, rsd.record_time_session)
    return quads, dict(spec=spec, bank=bank, offset=off, centres=centres)


def _group_index(session, sd, quads) -> dict:
    """Auswertegruppen → Liste (Block, Indexmaske) für die Rekonstruktion."""
    G: dict[str, list] = {}
    for r in session.runs:
        b = r.block_id
        if b not in quads:
            continue
        rq = quads[b]
        gm = group_mask(r, sd.runs[b])
        main = gm["main"] & rq.clean if r.dataset_type in ("heralded", "herald_blocked") else gm["main"] & (rq.coverage >= 0.99)
        key = {"heralded": "A", "free_running": "B", "random_trigger": "B", "vacuum": "KV", "herald_blocked": "KH",
               "state": "state", "electronic": "EL"}.get(r.dataset_type, r.dataset_type)
        if r.dataset_type == "random_trigger":
            key = "B"
        G.setdefault(key, []).append((b, main))
        if "random" in gm:
            G.setdefault("B_interleaved", []).append((b, gm["random"] & (rq.coverage >= 0.99)))
    return G


def _est(ap: AnalysisParams, n: int) -> str:
    return "binned" if (ap.estimator == "maxlik" and n > ap.binned_above) else ap.estimator


# ---------------------------------------------------------------- Bootstrap
class Normalizer:
    """Normierte Quadraturen aller Gruppen; im Bootstrap mit neu gezogenen Vakuumdaten (gemeinsam für A und B)."""

    def __init__(self, cal: Calibration, quads: dict, groups: dict):
        self.cal, self.quads, self.groups = cal, quads, groups
        self.kv_blocks = [r["block"] for r in cal.kv_runs]

    def x(self, key, var_kv=None, kv_vars=None, q0=None):
        out = []
        for b, m in self.groups[key]:
            rq = self.quads[b]
            xx = self.cal.normalize(rq.q[m], rq.t_session[m], b, rq.lo_b[m], var_kv=var_kv, kv_vars=kv_vars)
            if q0 is not None:
                xx = xx - (q0 - self.cal.q0) * np.sqrt(0.5 / (self.cal.var_kv if var_kv is None else var_kv))
            out.append(xx)
        return np.concatenate(out) if out else np.empty(0)

    def vacuum_draw(self, rng):
        """Neue Vakuumkalibrierung aus K-V-Records (geschichtet je K-V-Lauf)."""
        qs, vs = [], []
        for b in self.kv_blocks:
            q = self.quads[b].q
            qq = q[rng.integers(0, len(q), len(q))]
            qs.append(qq)
        allq = np.concatenate(qs)
        q0 = float(allq.mean())
        var = float(np.mean((allq - q0) ** 2))
        kv_vars = np.array([np.mean((qq - q0) ** 2) for qq in qs])
        return var, kv_vars, q0


def bootstrap_groups(norm: Normalizer, keys, point: dict, ap: AnalysisParams, rng) -> dict:
    """Gemeinsamer Bootstrap: je Ziehung dieselbe Vakuumkalibrierung für alle Gruppen, Records je Gruppe neu gezogen."""
    base = {k: norm.x(k) for k in keys}
    res = {k: {q: [] for q in ("var_x", "W00", "W00_moments", "eta_fit", "eta_var", "mean_n", "p1")} for k in keys}
    res["diff"] = {q: [] for q in ("var_x", "W00", "W00_moments", "eta_fit")}
    for _ in range(ap.n_boot):
        if ap.boot_vacuum:
            var_kv, kv_vars, q0 = norm.vacuum_draw(rng)
            xs = {k: norm.x(k, var_kv=var_kv, kv_vars=kv_vars, q0=q0) for k in keys}
        else:
            xs = base
        st = {}
        for k in keys:
            x = xs[k]
            xb = x[rng.integers(0, len(x), len(x))]
            g = group_stats(xb, ap.nmax, _est(ap, len(xb)), p0=point[k]["_p"], full=False, tol=max(ap.tol, 1e-7))
            st[k] = g
            for q in ("var_x", "W00", "W00_moments", "eta_fit", "eta_var"):
                res[k][q].append(g[q])
            res[k]["mean_n"].append(float(np.dot(np.arange(len(g["_p"])), g["_p"])))
            res[k]["p1"].append(g["p_n"][1])
        if "A" in st and "B" in st:
            for q in res["diff"]:
                res["diff"][q].append(st["A"][q] - st["B"][q])
    return res


def _summ(v, point):
    v = np.asarray(v)
    return dict(err=float(np.std(v, ddof=1)), mean_boot=float(np.mean(v)), bias_boot=float(np.mean(v) - point),
                ci68=[float(np.percentile(v, 16)), float(np.percentile(v, 84))],
                ci95=[float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5))])


# ---------------------------------------------------------------- Vergleich A/B
def compare_ab(xA, xB, gA, gB, boot, ap: AnalysisParams) -> dict:
    from scipy import stats as S
    edges = np.linspace(-ap.hist_lim, ap.hist_lim, ap.hist_bins + 1)
    hA, _ = np.histogram(xA, edges); hB, _ = np.histogram(xB, edges)
    w = np.diff(edges)
    dA, dB = hA / (len(xA) * w), hB / (len(xB) * w)
    eA, eB = np.sqrt(hA) / (len(xA) * w), np.sqrt(hB) / (len(xB) * w)
    ks = S.ks_2samp(xA, xB)
    m = (hA + hB) > 0
    chi2 = float(np.sum((dA - dB)[m] ** 2 / (eA ** 2 + eB ** 2)[m]))
    out = dict(
        histogram=dict(edges=edges.tolist(), A=dA.tolist(), B=dB.tolist(), A_err=eA.tolist(), B_err=eB.tolist(),
                       chi2=chi2, ndof=int(m.sum())),
        ks=dict(statistic=float(ks.statistic), pvalue=float(ks.pvalue)),
        wigner_r=gA["wigner_r"], wigner_diff=(np.array(gA["wigner_radial"]) - np.array(gB["wigner_radial"])).tolist(),
    )
    for q in ("var_x", "W00", "W00_moments", "eta_fit"):
        d = gA[q] - gB[q]
        e = float(np.std(boot["diff"][q], ddof=1)) if boot and boot["diff"][q] else float("nan")
        out[f"diff_{q}"] = dict(value=float(d), err=e, z=float(d / e) if e > 0 else float("nan"))
    # Effizienz aus dem Vergleich: Var_A − Var_B = η_eff (Einzelphoton, B als Referenz statt ½)
    out["eta_from_comparison"] = dict(value=out["diff_var_x"]["value"], err=out["diff_var_x"]["err"],
                                      model="Var_A − Var_B (verlustbehaftetes |1⟩, B ≈ Vakuum + n_B)")
    return out


# ---------------------------------------------------------------- Drift (A-B-A, Hälften)
def drift_ab(session, norm: Normalizer, ap: AnalysisParams) -> dict:
    """Je Lauf Var x (analytischer Fehler); A-B-A-Folgen, Vergleich erste ↔ zweite Hälfte der A-Läufe."""
    from ..recon.stats import var_with_err, w00_linear
    per = []
    for r in session.runs:
        for key in ("A", "B"):
            for b, m in norm.groups.get(key, []):
                if b != r.block_id:
                    continue
                rq = norm.quads[b]
                x = norm.cal.normalize(rq.q[m], rq.t_session[m], b, rq.lo_b[m])
                v, ve = var_with_err(x); w, we = w00_linear(x)
                per.append(dict(block=b, group=key, t=float(r.t_start_s + r.duration_s / 2), n=int(len(x)),
                                var_x=v, var_err=ve, W00_moments=w, W00_moments_err=we))
    out: dict = dict(per_run=per, flags=[])
    A = [p for p in per if p["group"] == "A"]
    B = [p for p in per if p["group"] == "B"]

    def pooled(ps):
        w = np.array([1 / p["var_err"] ** 2 for p in ps]); v = np.array([p["var_x"] for p in ps])
        return float(np.sum(w * v) / np.sum(w)), float(1 / np.sqrt(np.sum(w)))

    for name, ps in (("A", A), ("B", B)):
        if len(ps) >= 2:
            mu, _ = pooled(ps)
            chi2 = float(sum((p["var_x"] - mu) ** 2 / p["var_err"] ** 2 for p in ps))
            out[f"chi2_runs_{name}"] = dict(chi2=chi2, ndof=len(ps) - 1)
    if len(A) >= 2:
        h = len(A) // 2
        first, second = A[:h], A[len(A) - h:] if len(A) % 2 == 0 else A[h + 1:]
        if len(A) % 2 == 1:
            second = A[h + 1:]; first = A[:h]
        m1, e1 = pooled(first); m2, e2 = pooled(second)
        z = (m2 - m1) / np.hypot(e1, e2)
        out["A_halves"] = dict(first=[p["block"] for p in first], second=[p["block"] for p in second],
                               var_first=m1, var_second=m2, diff=m2 - m1, err=float(np.hypot(e1, e2)), z=float(z))
        if abs(z) > ap.drift_z:
            out["flags"].append(f"A-Läufe: Varianz erste/zweite Hälfte unterscheidet sich (z = {z:+.1f})")
    # A-B-A: jedes B gegen das Mittel der benachbarten A
    order = sorted(per, key=lambda p: p["t"])
    aba = []
    for i, p in enumerate(order):
        if p["group"] != "B":
            continue
        prev = next((q for q in reversed(order[:i]) if q["group"] == "A"), None)
        nxt = next((q for q in order[i + 1:] if q["group"] == "A"), None)
        if prev and nxt:
            dA = nxt["var_x"] - prev["var_x"]; eA = np.hypot(prev["var_err"], nxt["var_err"])
            aba.append(dict(B=p["block"], A_before=prev["block"], A_after=nxt["block"], dA=float(dA), err=float(eA),
                            z=float(dA / eA)))
    out["ABA"] = aba
    if aba:
        zz = np.array([a["z"] for a in aba])
        out["ABA_chi2"] = dict(chi2=float(np.sum(zz ** 2)), ndof=len(zz), max_abs_z=float(np.max(np.abs(zz))))
        if np.max(np.abs(zz)) > ap.drift_z + 0.5:
            out["flags"].append(f"A-B-A: A-Varianz ändert sich über ein B hinweg (max |z| = {np.max(np.abs(zz)):.1f})")
    return out


# ---------------------------------------------------------------- Effizienzbudget
def efficiency_budget(session, sd, cal: Calibration, res_groups: dict, pca: dict | None, comp: dict | None,
                      ap: AnalysisParams) -> dict:
    n_her = 0; dur = 0.0
    for r in session.runs_of("heralded"):
        rep = sd.runs[r.block_id].report.get("assignment", {}).get("heralds", {})
        n_her += sum(int(v) for v in rep.values()); dur += r.duration_s
    R = n_her / dur if dur > 0 else float("nan")
    xi = 1 - ap.dark_count_rate / R if R > 0 else float("nan")
    A = res_groups.get("A")
    out = dict(herald_rate_per_s=R, dark_count_rate_per_s=ap.dark_count_rate,
               xi=xi, eta_el=cal.eta_el if cal.var_el is not None else None,
               eta_mode_pca=(pca or {}).get("overlap_param_corrected"))
    if A:
        ef = A["eta_fit"]; efe = A.get("eta_fit_err", float("nan"))
        out["eta_eff_fit"] = dict(value=ef, err=efe)
        out["eta_eff_var"] = dict(value=A["eta_var"], err=A.get("var_x_err", float("nan")))
        if comp:
            out["eta_eff_comparison"] = comp["eta_from_comparison"]
        den = xi * (cal.eta_el if cal.var_el is not None else 1.0)
        out["eta_rest"] = dict(value=ef / den, err=efe / den,
                               note="η_eff/(ξ·η_el): optische Verluste, Detektor-QE, Modenanpassung"
                                    + ("" if cal.var_el is not None else " (η_el unbekannt, nicht abgezogen)"))
    return out


# ---------------------------------------------------------------- Kette
def analyze_session(path, ap: AnalysisParams | None = None, out_dir=None, sync_params=None, write: bool = True,
                    stages=("calibrate", "reconstruct", "compare")) -> dict:
    from ..io.session import load_session
    ap = ap or AnalysisParams()
    t0 = time.time(); timing = {}
    session = load_session(path)
    sd = ensure_sync(session, sync_params)
    timing["load_sync"] = time.time() - t0
    t = time.time()
    quads, ex = extract_session(session, sd, ap)
    timing["extract"] = time.time() - t
    spec = ex["spec"]
    groups = _group_index(session, sd, quads)
    warnings: list[str] = []
    # Kalibrierung (vorläufig, für PCA-Skala)
    t = time.time()
    cal, drift_kv = calibrate(session, quads, ap.calib_params())
    timing["calibrate"] = time.time() - t
    pca = None
    if ap.pca and "A" in groups and "KV" in groups:
        t = time.time()
        runs = {r.block_id: r for r in session.runs}
        gA = [(runs[b], np.flatnonzero(m), ex["centres"][b]) for b, m in groups["A"]]
        gV = [(runs[b], np.arange(runs[b].n_records), ex["centres"][b]) for b, _ in groups["KV"]]
        var_sample = _vacuum_sample_var(gV, ex["offset"])
        pca = pca_mode(gA, gV, ex["offset"], var_sample, spec)
        timing["pca"] = time.time() - t
        if not pca["available"]:
            warnings.append("PCA nicht möglich: " + pca["reason"])
            pca = None
        if pca and ap.mode_source in ("pca", "pca_raw"):
            spec = pca_fitted_template(pca, spec) if ap.mode_source == "pca" else pca_template(pca, spec)
            if ap.mode_source == "pca_raw":
                warnings.append("Mode aus roher PCA-Eigenmode: Varianz durch Auswahl an denselben Daten verzerrt")
            quads, ex = extract_session(session, sd, ap, spec)
            cal, drift_kv = calibrate(session, quads, ap.calib_params())
    xt = crosstalk_check(session, quads, cal, ap.calib_params())
    warnings += cal.warnings + xt.get("flags", [])
    norm = Normalizer(cal, quads, groups)
    result: dict = dict(schema=SCHEMA, software=dict(name="hhd", version=__version__),
                        session=str(session.path), session_id=session.session_id,
                        campaign_id=session.campaign_id, calibration_id=session.calibration_id,
                        params=asdict(ap), mode=dict(spec.to_dict(), bandwidth_effect=float(ex["bank"].bandwidth_effect),
                                                     adc_offset_lsb=ex["offset"]),
                        calibration=cal.to_dict(), drift_vacuum_lo=drift_kv, crosstalk=xt, pca=_pca_brief(pca),
                        groups={k: dict(runs=[b for b, _ in v], n=int(sum(m.sum() for _, m in v))) for k, v in groups.items()})
    rng = np.random.default_rng(ap.seed)
    res_groups = {}
    comp = None
    if "reconstruct" in stages:
        t = time.time()
        keys = [k for k in ("A", "B", "B_interleaved", "KH", "state") if k in groups and norm.x(k).size > 10]
        xs0 = {k: norm.x(k) for k in keys}
        point = {k: group_stats(xs0[k], ap.nmax, _est(ap, len(xs0[k])), tol=ap.tol) for k in keys}
        timing["reconstruct"] = time.time() - t
        t = time.time()
        bkeys = [k for k in ("A", "B", "state") if k in point]
        boot = bootstrap_groups(norm, bkeys, point, ap, rng) if ap.n_boot > 1 else None
        timing["bootstrap"] = time.time() - t
        for k, g in point.items():
            gg = {q: v for q, v in g.items() if q != "_p"}
            if boot and k in boot:
                for q in ("var_x", "W00", "W00_moments", "eta_fit", "eta_var", "mean_n", "p1"):
                    pv = g[q] if q not in ("mean_n", "p1") else (g["mean_n"] if q == "mean_n" else g["p_n"][1])
                    s = _summ(boot[k][q], pv)
                    gg[f"{q}_err"] = s["err"]; gg[f"{q}_boot"] = s
                if ap.bias_correct:
                    gg["W00_bc"] = g["W00"] - gg["W00_boot"]["bias_boot"]
            res_groups[k] = gg
        result["reconstruction"] = res_groups
        result["drift_AB"] = drift_ab(session, norm, ap)
        warnings += result["drift_AB"]["flags"]
        if "compare" in stages and "A" in point and "B" in point:
            t = time.time()
            comp = compare_ab(norm.x("A"), norm.x("B"), point["A"], point["B"], boot, ap)
            result["comparison"] = comp
            timing["compare"] = time.time() - t
        result["efficiency_budget"] = efficiency_budget(session, sd, cal, res_groups, pca, comp, ap)
    result["warnings"] = warnings
    timing["total"] = time.time() - t0
    result["performance"] = dict(timing_s=timing, n_records=int(sum(len(q.q) for q in quads.values())))
    if write:
        od = Path(out_dir) if out_dir else Path(session.path) / "analysis"
        od.mkdir(parents=True, exist_ok=True)
        (od / "result.json").write_text(json.dumps(_clean(result), indent=1, ensure_ascii=False), encoding="utf-8")
        write_quadratures(od / "quadratures.h5", quads, cal, groups)
        from .report import write_report
        write_report(result, od / "report.md")
        result["_out_dir"] = str(od)
    return result


def _vacuum_sample_var(gV, offset, max_records=2000):
    s = 0.0; n = 0
    for run, idx, _ in gV:
        d = run.read_records(idx[:max_records]).astype(np.float64) - offset
        s += float(np.sum(d ** 2)); n += d.size
    return s / n


def _pca_brief(pca):
    if pca is None:
        return None
    return {k: v for k, v in pca.items()}


def write_quadratures(path, quads, cal, groups):
    with h5py.File(path, "w") as f:
        f.attrs["schema"] = "hhd-quadratures/1.0"
        f.attrs["calibration_json"] = json.dumps(_clean(cal.to_dict()), ensure_ascii=False)
        for b, rq in quads.items():
            g = f.create_group(f"runs/{b}")
            g.attrs["dataset_type"] = rq.dataset_type
            g["q_raw_lsb"] = rq.q
            if rq.dataset_type != "electronic":
                g["x"] = cal.normalize(rq.q, rq.t_session, b, rq.lo_b)
            g["coverage"] = rq.coverage.astype(np.float32)
            g["clean"] = rq.clean
            g["seg_class"] = rq.seg_class
            g["t_session_s"] = rq.t_session
            g["lo_b_lsb"] = rq.lo_b.astype(np.float32)
        for k, v in groups.items():
            for b, m in v:
                f[f"groups/{k}/{b}"] = m


def _clean(o):
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items() if not str(k).startswith("_")}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return _clean(o.tolist())
    if isinstance(o, (np.floating,)):
        o = float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, float) and not np.isfinite(o):
        return None
    return o
