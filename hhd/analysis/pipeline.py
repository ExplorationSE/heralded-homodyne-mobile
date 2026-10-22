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
from ..recon.core import photon_stats
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
    pca_max_records: int = 20000            # je Gruppe (A, K-V); ≤ 0: alle (Datenstrom: inkrementelle Kovarianz)
    norm: str = "pooled"
    clearance_dB: float | None = None
    estimator: str = "maxlik"
    nmax: int = 10
    tol: float = 1e-8
    binned_above: int = 50000               # ab dieser Gruppengröße Histogramm-MaxLik (gleiches Ergebnis, ~100× schneller)
    n_boot: int = 200
    boot_vacuum: bool = True                # OP-1: Vakuumnormierung im Bootstrap neu ziehen
    bias_correct: bool = True               # OP-2: Bootstrap-Biaskorrektur für W(0,0)
    ci_method: str = "auto"                 # Konfidenzintervall: auto (je Größe, siehe CI_DEFAULT) oder eine Methode
    jackknife_groups: int = 20              # Gruppen-Jackknife für die BCa-Beschleunigung (0 = aus)
    seed: int = 1
    dark_count_rate: float = 100.0          # Herald-Dunkelzählrate (1/s) für ξ im Effizienzbudget
    drift_z: float = 3.0                    # OP-6: Driftschwelle |z| (A-Hälften, Nulltests; A-B-A: drift_z + 0,5)
    drift_p: float = 0.0027                 # OP-6: p-Schwelle (χ² der Läufe, A-B-A, KS der Nulltests) ≙ 3σ
    hist_bins: int = 60
    hist_lim: float = 5.0
    # phasenaufgelöste Auswertung (ab 0.15; nur mit Rampenspur und Lauf K-θ, sonst phasengemittelt)
    phase: bool = True
    phase_ref_max_records: int = 50000      # je K-θ-Lauf für den Kalibrierfit
    phase_cal_deg: int = 3                  # Polynomgrad V → θ je Rampenrichtung
    theta_noise_prior_rad: float = 0.03     # angenommenes Phasenrauschen je Record (Untergrenze in σ_θ; aus K-θ kaum messbar)
    theta_sign: int = 1                     # +1: θ steigt mit der Rampenspannung (Vorzeichen nicht messbar, Aufbau)
    tomo_eta_extra: float = 1.0             # zusätzlich bekannte Detektionseffizienz in der POVM (× η_el)
    tomo_nmax: int = 30
    tomo_theta_bins: int = 32
    tomo_dx: float = 0.15
    tomo_max_iter: int = 5000
    tomo_boot_iter: int = 800
    n_boot_tomo: int = 20
    var_theta_bins: int = 24
    wigner2d_points: int = 61
    # Mehrkern (ab 0.15): Bootstrap/Jackknife-Ziehungen auf Prozesse verteilt (hhd.parallel). 0 = alle Kerne
    # (os.sched_getaffinity/os.cpu_count), 1 = seriell; Umgebung HHD_WORKERS hat Vorrang. Unter parallel_min_values
    # (n_boot · Σ Gruppengrößen) bleibt es seriell, solange kein Pool läuft (Start ≈ 0,7 s lohnt dann nicht).
    # Die Zufallszahlen werden immer im Hauptprozess in derselben Reihenfolge gezogen → Ergebnis unabhängig von der
    # Worker-Zahl (reproduzierbar mit seed).
    workers: int = 0
    parallel_min_values: int = 1_000_000

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


def _boot_job(job: tuple) -> dict:
    """Worker (Prozess-Pool, spawn): eine Bootstrap-/Jackknife-Rekonstruktion."""
    xb, nmax, est, p0, tol = job
    return group_stats(xb, nmax, est, p0=p0, full=False, tol=tol)


def _n_workers(ap: AnalysisParams, n_values: int) -> int:
    from ..parallel import pool_running, resolve_workers
    nw = resolve_workers(ap.workers)
    if nw > 1 and n_values < ap.parallel_min_values and not pool_running():
        return 1
    return nw


def bootstrap_groups(norm: Normalizer, keys, point: dict, ap: AnalysisParams, rng) -> dict:
    """Gemeinsamer Bootstrap: je Ziehung dieselbe Vakuumkalibrierung für alle Gruppen, Records je Gruppe neu gezogen.

    Die Ziehungen (Vakuum, Indizes) entstehen im Hauptprozess in fester Reihenfolge aus ``rng``; nur die
    Rekonstruktionen laufen parallel (``ap.workers``), die Ergebnisse kommen in Eingabereihenfolge zurück."""
    from ..parallel import ordered_map
    base = {k: norm.x(k) for k in keys}
    res = {k: {q: [] for q in ("var_x", "W00", "W00_moments", "eta_fit", "eta_var", "mean_n", "p1",
                               "mandel_Q", "g2", "var_x_se", "W00_moments_se", "eta_fit_se")} for k in keys}
    res["diff"] = {q: [] for q in ("var_x", "W00", "W00_moments", "eta_fit")}
    nw = _n_workers(ap, ap.n_boot * sum(len(v) for v in base.values()))

    def jobs():
        for _ in range(ap.n_boot):
            if ap.boot_vacuum:
                var_kv, kv_vars, q0 = norm.vacuum_draw(rng)
                xs = {k: norm.x(k, var_kv=var_kv, kv_vars=kv_vars, q0=q0) for k in keys}
            else:
                xs = base
            for k in keys:
                x = xs[k]
                xb = x[rng.integers(0, len(x), len(x))]
                yield xb, ap.nmax, _est(ap, len(xb)), point[k]["_p"], max(ap.tol, 1e-7)

    out = ordered_map(_boot_job, jobs(), nw)
    for _ in range(ap.n_boot):
        st = {}
        for k in keys:
            g = next(out)
            st[k] = g
            for q in ("var_x", "W00", "W00_moments", "eta_fit", "eta_var"):
                res[k][q].append(g[q])
            res[k]["mean_n"].append(float(np.dot(np.arange(len(g["_p"])), g["_p"])))
            res[k]["var_x_se"].append(g["var_err"]); res[k]["W00_moments_se"].append(g["W00_moments_err"])
            res[k]["eta_fit_se"].append(g["eta_fit_err_fisher"])
            res[k]["p1"].append(g["p_n"][1])
            ps_ = photon_stats(np.asarray(g["p_n"], float))
            res[k]["mandel_Q"].append(ps_["mandel_Q"]); res[k]["g2"].append(ps_["g2"])
        if "A" in st and "B" in st:
            for q in res["diff"]:
                res["diff"][q].append(st["A"][q] - st["B"][q])
    return res


CI_QUANT = ("var_x", "W00", "W00_moments", "eta_fit", "eta_var")
SE_KEY = {"var_x": ("var_x_se", "var_err"), "eta_var": ("var_x_se", "var_err"),
          "W00_moments": ("W00_moments_se", "W00_moments_err"), "eta_fit": ("eta_fit_se", "eta_fit_err_fisher")}
# Standardmethode je Größe (Kampagnenstudie 0.8.0, docs/ANALYSE.md Abschnitt 4)
# Standard-KI je Größe (0.8.0, Kampagne 264 Sitzungen = 200 neu + 64 aus 0.7, n_boot 200): die symmetrische
# Normal-Näherung θ̂ ± z·sd(θ*) trifft 68/95 % am besten (W(0,0): 68,2/94,7 %, η_Fit: 68,2/93,6 %, Var x: 67,0/96,6 %);
# BCa/Perzentil liegen gleichauf bzw. bei 95 % etwas darunter, studentisiert überdeckt (Var x 87/99,6 %).
CI_DEFAULT = {"var_x": "normal", "eta_var": "normal", "eta_fit": "normal", "W00": "normal", "W00_moments": "normal"}


def jackknife_values(norm: Normalizer, key: str, point: dict, ap: AnalysisParams, rng) -> dict:
    from ..parallel import ordered_map
    from ..recon.intervals import jackknife_groups
    x = norm.x(key)
    out = {q: [] for q in CI_QUANT}
    masks = list(jackknife_groups(len(x), ap.jackknife_groups, rng))
    nw = _n_workers(ap, len(masks) * len(x))
    jobs = ((x[m], ap.nmax, _est(ap, int(m.sum())), point["_p"], max(ap.tol, 1e-7)) for m in masks)
    for g in ordered_map(_boot_job, jobs, nw):
        for q in CI_QUANT:
            out[q].append(g[q])
    return out


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


def null_tests(norm: "Normalizer") -> dict:
    """OP-5: A-A und B-B (Läufe abwechselnd in zwei Hälften) als Nullhypothese neben A-B (KS-p-Wert, Var-Differenz)."""
    from scipy import stats as S
    from ..recon.stats import var_with_err
    out = {}
    for key in ("A", "B"):
        G = norm.groups.get(key, [])
        if len(G) < 2:
            continue
        xs = []
        for b, m in G:
            rq = norm.quads[b]
            xs.append(norm.cal.normalize(rq.q[m], rq.t_session[m], b, rq.lo_b[m]))
        h1 = np.concatenate(xs[0::2]); h2 = np.concatenate(xs[1::2])
        v1, e1 = var_with_err(h1); v2, e2 = var_with_err(h2)
        out[key + key] = dict(runs_1=[b for b, _ in G[0::2]], runs_2=[b for b, _ in G[1::2]], n1=len(h1), n2=len(h2),
                              ks_p=float(S.ks_2samp(h1, h2).pvalue), var_diff=v1 - v2,
                              var_z=float((v1 - v2) / np.hypot(e1, e2)))
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
    return drift_ab_from_per(per, ap)


def drift_ab_from_per(per: list[dict], ap: AnalysisParams) -> dict:
    """Driftprüfungen aus den Kenngrößen je Lauf (auch für die Datenstrom-Auswertung)."""
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
            from scipy.stats import chi2 as _chi2
            pv = float(_chi2.sf(chi2, len(ps) - 1))
            out[f"chi2_runs_{name}"] = dict(chi2=chi2, ndof=len(ps) - 1, p=pv)
            if pv < ap.drift_p:
                out["flags"].append(f"{name}-Läufe: Varianzen streuen mehr als statistisch erwartet "
                                    f"(χ² = {chi2:.1f}/{len(ps) - 1}, p = {pv:.1e})")
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
        from scipy.stats import chi2 as _chi2
        pa = float(_chi2.sf(np.sum(zz ** 2), len(zz)))
        out["ABA_chi2"] = dict(chi2=float(np.sum(zz ** 2)), ndof=len(zz), max_abs_z=float(np.max(np.abs(zz))), p=pa)
        if np.max(np.abs(zz)) > ap.drift_z + 0.5 or pa < ap.drift_p:
            out["flags"].append(f"A-B-A: A-Varianz ändert sich über ein B hinweg (max |z| = {np.max(np.abs(zz)):.1f})")
    return out


# ---------------------------------------------------------------- Effizienzbudget
def session_state(session) -> dict:
    """Geheraldeten Zustand aus den Laufattributen lesen (Simulator-Laufplan ``kurz_<zustand>``).

    Fehlt die Angabe (Messdaten, ältere Sitzungen), gilt das Einzelphoton |1⟩. Nur die Beschreibung des
    vorgesehenen Zustands (ideal, ohne Verluste), keine Wahrheitsdaten.
    """
    for r in session.runs_of("heralded"):
        raw = r.attrs.get("heralded_state", "")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if raw:
            try:
                return json.loads(raw)
            except ValueError:
                pass
    return dict(preset="fock1", label="Einzelphoton |1⟩", phase_sensitive=False, mean_n_ideal=1.0,
                phase_averaged=True, assumed=True)


def efficiency_budget(session, sd, cal: Calibration, res_groups: dict, pca: dict | None, comp: dict | None,
                      ap: AnalysisParams) -> dict:
    n_her = 0; dur = 0.0
    for r in session.runs_of("heralded"):
        rep = sd.runs[r.block_id].report.get("assignment", {}).get("heralds", {})
        n_her += sum(int(v) for v in rep.values()); dur += r.duration_s
    R = n_her / dur if dur > 0 else float("nan")
    xi = 1 - ap.dark_count_rate / R if R > 0 else float("nan")
    A = res_groups.get("A")
    st = session_state(session)
    fock1 = st.get("preset", "fock1") == "fock1"
    n_id = float(st.get("mean_n_ideal") or 1.0)
    out = dict(state=st, herald_rate_per_s=R, dark_count_rate_per_s=ap.dark_count_rate,
               xi=xi, eta_el=cal.eta_el if cal.var_el is not None else None,
               eta_mode_pca=(pca or {}).get("overlap_param_corrected"))
    if A:
        ef = A["eta_fit"]; efe = A.get("eta_fit_err", float("nan"))
        out["eta_eff_fit"] = dict(value=ef, err=efe)
        out["eta_eff_var"] = dict(value=A["eta_var"], err=A.get("var_x_err", float("nan")))
        if comp:
            out["eta_eff_comparison"] = comp["eta_from_comparison"]
        if not fock1:
            # η aus der phasengemittelten Varianz: Var x − ½ = η_eff·⟨n⟩_ideal (für jeden Zustand, auch Gauß).
            # Der |1⟩-Verlustfit (eta_fit) gilt hier nicht und wird nur als modellabhängig mitgeführt.
            ef = (A["var_x"] - 0.5) / n_id if n_id > 0 else float("nan")
            efe = A.get("var_x_err", float("nan")) / n_id if n_id > 0 else float("nan")
            A["eta_n"], A["eta_n_err"] = ef, efe
            out["eta_eff_fit"]["note"] = "|1⟩-Verlustmodell, für diesen Zustand nicht gültig"
            out["eta_eff_n"] = dict(value=ef, err=efe, note="(Var x − ½)/⟨n⟩_ideal, phasengemittelt")
            if comp:
                c = comp["eta_from_comparison"]
                out["eta_eff_comparison"] = dict(c, value=c["value"] / n_id, err=c["err"] / n_id)
        den = xi * (cal.eta_el if cal.var_el is not None else 1.0)
        rel_el = (cal.eta_el_err or 0.0) / cal.eta_el if cal.var_el is not None else 0.0
        out["eta_el_err"] = cal.eta_el_err
        out["eta_rest"] = dict(value=ef / den, err=float(np.hypot(efe / den, ef / den * rel_el)),
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
        pca = pca_mode(gA, gV, ex["offset"], var_sample, spec, max_records=ap.pca_max_records)
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
                for q in ("mandel_Q", "g2"):               # Mandel Q / g²(0) aus p_n je Ziehung (nan bei ⟨n⟩ ≈ 0)
                    v = np.asarray(boot[k].get(q, []), float); v = v[np.isfinite(v)]
                    gg[f"{q}_err"] = float(np.std(v, ddof=1)) if len(v) > 2 else float("nan")
                    gg[f"{q}_n_boot"] = int(len(v))
                from ..recon.intervals import intervals
                jk = jackknife_values(norm, k, g, ap, rng) if ap.jackknife_groups >= 3 else {}
                cis = {}
                for q in CI_QUANT:
                    sk = SE_KEY.get(q)
                    ci = intervals(g[q], boot[k][q], boot[k][sk[0]] if sk else None, g[sk[1]] if sk else None,
                                   jk.get(q))
                    if not ci:
                        continue
                    cis[q] = {m: v for m, v in ci.items() if not m.startswith("_")}
                    meth = CI_DEFAULT.get(q, "percentile") if ap.ci_method == "auto" else ap.ci_method
                    meth = meth if meth in cis[q] else "percentile"
                    gg[f"{q}_ci68"], gg[f"{q}_ci95"] = cis[q][meth]["68"], cis[q][meth]["95"]
                    gg[f"{q}_ci_method"] = meth
                    gg[f"{q}_ci_params"] = ci["_params"]
                gg["ci_all"] = cis
                if ap.bias_correct:
                    gg["W00_bc"] = g["W00"] - gg["W00_boot"]["bias_boot"]
            res_groups[k] = gg
        result["reconstruction"] = res_groups
        result["drift_AB"] = drift_ab(session, norm, ap)
        warnings += result["drift_AB"]["flags"]
        if "compare" in stages and "A" in point and "B" in point:
            t = time.time()
            comp = compare_ab(norm.x("A"), norm.x("B"), point["A"], point["B"], boot, ap)
            comp["null_tests"] = null_tests(norm)
            for nk, nt in comp["null_tests"].items():
                if abs(nt["var_z"]) > ap.drift_z or nt["ks_p"] < ap.drift_p:
                    warnings.append(f"Nulltest {nk}: Lauf-Hälften unterscheiden sich (Var z = {nt['var_z']:+.1f}, "
                                    f"KS p = {nt['ks_p']:.1e})")
            result["comparison"] = comp
            timing["compare"] = time.time() - t
        result["efficiency_budget"] = efficiency_budget(session, sd, cal, res_groups, pca, comp, ap)
    result["state"] = session_state(session)
    if "reconstruct" in stages and ap.phase:
        t = time.time()
        from .phase import MemorySource, annotate_warnings, phase_analysis
        od_ph = (Path(out_dir) if out_dir else Path(session.path) / "analysis") if write else None
        if od_ph is not None:
            od_ph.mkdir(parents=True, exist_ok=True)
        result["phase"] = phase_analysis(session, MemorySource(quads, cal, groups), cal, ap,
                                         (result.get("efficiency_budget") or {}).get("xi"), result["state"], od_ph)
        warnings += result["phase"].get("warnings", [])
        warnings[:] = annotate_warnings(warnings, result["phase"], result["state"])
        timing["phase"] = time.time() - t
    result["warnings"] = warnings
    timing["total"] = time.time() - t0
    from ..io.h5filters import status as _h5status
    from ..parallel import available_cpus, resolve_workers
    result["performance"] = dict(timing_s=timing, n_records=int(sum(len(q.q) for q in quads.values())),
                                 workers=resolve_workers(ap.workers), cpus=available_cpus(),
                                 maxlik_accel=bool(_accel()), h5=_h5status())
    if write:
        od = Path(out_dir) if out_dir else Path(session.path) / "analysis"
        od.mkdir(parents=True, exist_ok=True)
        (od / "result.json").write_text(json.dumps(_clean(result), indent=1, ensure_ascii=False), encoding="utf-8")
        write_quadratures(od / "quadratures.h5", quads, cal, groups)
        from .report import write_report
        write_report(result, od / "report.md")
        result["_out_dir"] = str(od)
    return result


def _accel() -> bool:
    from ..recon import core
    return core.ACCEL


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
