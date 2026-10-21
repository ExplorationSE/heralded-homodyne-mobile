"""Phasenaufgelöste Auswertung (ab 0.15): K-θ-Kalibrierung, θ je Record, MaxLik-Tomografie mit bekannter Phase.

Ablauf (gemeinsam für die In-Memory- und die Datenstrom-Auswertung, speicherbegrenzt):

1. Läufe K-θ (``phase_reference``) mit Rampenspur ``records/ramp_raw`` → :func:`hhd.calib.phase_cal.fit_phase_calibration`.
2. θ, σ_θ und Herkunft je Record für alle Läufe mit Rampenspur → ``analysis/phase.h5``
   (``runs/<block>/{theta, theta_err, theta_source}``; Herkunft 0 none, 1 ramp, 2 locked, 3 estimated).
3. A-Records (herald-bedingt, sauber) in ein θ×x-Histogramm (:class:`hhd.recon.tomo.PhaseHist`).
4. MaxLik (RρR) mit POVM inkl. η_el (elektronisches Rauschen als Verlust, wie in der phasengemittelten
   Auswertung); Abschneidegrenze automatisch; Poisson-Bootstrap des Histogramms für die Fehler.
   Ab 0.15 (Quellmodell): B-Records (ohne Herald, frei laufend/Zufall) getrennt in ein zweites Histogramm →
   Var(x_θ)-Fit (Quetschung in dB am Detektor) und MaxLik-ρ des Zustands ohne Herald (``result["phase"]["B"]``).
5. Kennwerte: 2D-Wigner, W(0,0), Parität, Reinheit, Quetschung (dB, Achse) aus ρ und aus Var(x_θ) der Daten,
   α (Katze) aus ⟨a²⟩, Treue zum Sollzustand (ideal und Verlustmodell ξ·L_η(ρ_t) + (1−ξ)|0⟩⟨0|),
   Phasenabdeckung mit Warnung bei Lücken.

Ohne K-θ-Läufe oder Rampenspur bleibt es bei der phasengemittelten Auswertung (``available = False``).
Die Simulator-Wahrheit wird hier nicht gelesen.
"""
from __future__ import annotations

import json
from pathlib import Path

import h5py
import numpy as np

from ..calib.phase_cal import THETA_SOURCES, fit_phase_calibration, ramp_direction
from ..recon import tomo as T

PHASE_SCHEMA = "hhd-phase/1.0"


class MemorySource:
    """Normierte Quadraturen aus der In-Memory-Auswertung (quads, Kalibrierung, Gruppenmasken)."""

    def __init__(self, quads, cal, groups):
        self.quads, self.cal = quads, cal
        self.masks = {}
        for k, v in groups.items():
            for b, m in v:
                if k in ("A", "B", "phase_reference"):
                    self.masks[b] = m

    def has(self, b):
        return b in self.quads

    def arrays(self, b):
        rq = self.quads[b]
        x = self.cal.normalize(rq.q, rq.t_session, b, rq.lo_b)
        m = self.masks.get(b, np.zeros(len(x), bool))
        return x, m, rq.t_session


class FileSource:
    """Normierte Quadraturen aus ``analysis/quadratures.h5`` der Datenstrom-Auswertung (Spalten x, main, t_session_s)."""

    def __init__(self, path):
        self.path = Path(path)

    def has(self, b):
        with h5py.File(self.path, "r") as f:
            return f"runs/{b}/x" in f

    def arrays(self, b):
        with h5py.File(self.path, "r") as f:
            g = f[f"runs/{b}"]
            return g["x"][:], g["main"][:].astype(bool), g["t_session_s"][:]


def _attr(run, k, default=None):
    v = run.attrs.get(k, default)
    return v.decode() if isinstance(v, bytes) else v


def _boot(tm: T.Tomography, rho0, counts, n_boot, rng, target, model, max_iter):
    keys = ("W00", "parity", "purity", "mean_n", "squeezing_dB", "axis_rad", "alpha_from_a2", "mandel_Q", "F_ideal", "F_model")
    out = {k: [] for k in keys}
    for _ in range(n_boot):
        c = rng.poisson(counts)
        r = tm.fit(rho0=rho0, counts=c, max_iter=max_iter)
        m = T.state_metrics(r["rho"])
        for k in keys[:8]:
            out[k].append(m[k])
        out["F_ideal"].append(T.fidelity_max_rotation(r["rho"], target, pure=True)[0] if target is not None else np.nan)
        out["F_model"].append(T.fidelity_max_rotation(r["rho"], model)[0] if model is not None else np.nan)
    return {k: dict(err=float(np.nanstd(v, ddof=1)) if len(v) > 1 else float("nan"),
                    ci68=[float(np.nanpercentile(v, 16)), float(np.nanpercentile(v, 84))] if len(v) > 1 else None)
            for k, v in out.items()}


def phase_analysis(session, src, cal, ap, xi: float | None, state: dict | None, out_dir=None, rng=None) -> dict:
    """Phasenaufgelöste Auswertung; Ergebnis für ``result["phase"]``. Schreibt ``phase.h5`` nach ``out_dir``."""
    rng = rng or np.random.default_rng(ap.seed + 7)
    runs = [r for r in session.runs if src.has(r.block_id)]
    refs_runs = [r for r in runs if r.dataset_type == "phase_reference" and getattr(r, "has_ramp", False)]
    a_runs = [r for r in runs if r.dataset_type == "heralded"]
    if not getattr(ap, "phase", True):
        return dict(available=False, reason="abgeschaltet (AnalysisParams.phase = False)")
    if not a_runs or not any(getattr(r, "has_ramp", False) for r in a_runs):
        return dict(available=False, reason="keine Rampenspur in den A-Läufen (Phase unbekannt) – phasengemittelt")
    if not refs_runs:
        return dict(available=False, reason="kein Referenzlauf K-θ – Phase nicht kalibrierbar, phasengemittelt")
    r0 = refs_runs[0]
    V_max = float(_attr(r0, "phase_piezo_V_max", 10.0))
    waveform = str(_attr(r0, "phase_piezo_waveform", "triangle"))
    cache = {}
    refs = []
    for r in refs_runs:
        x, m, t = src.arrays(r.block_id)
        V = r.read_ramp()
        d = ramp_direction(V, waveform)
        cache[r.block_id] = (V, d)
        m = m & np.isfinite(x)
        if m.sum() > ap.phase_ref_max_records:
            idx = np.flatnonzero(m); keep = idx[np.linspace(0, len(idx) - 1, ap.phase_ref_max_records).astype(int)]
            m = np.zeros(len(m), bool); m[keep] = True
        refs.append(dict(block=r.block_id, x=x[m], V=V[m], d=d[m], t=t[m]))
    pc = fit_phase_calibration(refs, V_max=V_max, waveform=waveform, deg=ap.phase_cal_deg, sign=ap.theta_sign)
    warnings = list(pc.quality.get("warnings", []))
    # Phasenrauschen je Record ist mit K-θ (α ≈ 2, einige 10³ Records) kaum auflösbar → angenommene Untergrenze
    pc.quality["phase_noise_fit_rad"] = pc.phase_noise_rad
    pc.phase_noise_rad = max(pc.phase_noise_rad, float(ap.theta_noise_prior_rad))
    pc.quality["phase_noise_used_rad"] = pc.phase_noise_rad
    # θ je Record, phase.h5, Histogramm der A-Records
    H = T.PhaseHist(n_theta=96, dx=0.05, xmax=8.0)
    HB = T.PhaseHist(n_theta=96, dx=0.05, xmax=8.0)
    err_A, err_B = [], []
    ph_path = Path(out_dir) / "phase.h5" if out_dir else None
    f = h5py.File(ph_path, "w") if ph_path else None
    try:
        if f is not None:
            f.attrs.update(schema=PHASE_SCHEMA, calibration_json=json.dumps(_tolist(pc.to_dict()), ensure_ascii=False),
                           theta_sources=json.dumps(THETA_SOURCES), convention="x_θ = (a e^{-iθ} + a† e^{iθ})/√2; θ = 0: K-θ-Referenz")
        for r in runs:
            if not getattr(r, "has_ramp", False):
                continue
            b = r.block_id
            V, d = cache.get(b) or (lambda V: (V, ramp_direction(V, waveform)))(r.read_ramp())
            x, m, t = src.arrays(b)
            th, er = pc.theta(V, d, t)
            srcc = np.full(len(th), THETA_SOURCES["ramp"], np.uint8)
            if f is not None:
                g = f.create_group(f"runs/{b}")
                g.attrs["dataset_type"] = r.dataset_type
                g.create_dataset("theta", data=th.astype(np.float32)).attrs["units"] = "rad"
                g.create_dataset("theta_err", data=er.astype(np.float32)).attrs["units"] = "rad"
                g.create_dataset("theta_source", data=srcc)
            if r.dataset_type == "heralded":
                mm = m & np.isfinite(x)
                H.add(x[mm], th[mm])
                err_A.append(er[mm])
            elif r.dataset_type in ("free_running", "random_trigger"):
                mm = m & np.isfinite(x) & np.isfinite(th)
                HB.add(x[mm], th[mm])
                err_B.append(er[mm])
    finally:
        if f is not None:
            f.close()
    if H.n < 500:
        return dict(available=False, reason=f"zu wenige A-Records mit Phase ({H.n})", calibration=_tolist(pc.to_dict()))
    eA = np.concatenate(err_A) if err_A else np.empty(0)
    # Tomografie
    eta_el = cal.eta_el if getattr(cal, "var_el", None) is not None and cal.eta_el else 1.0
    eta = float(eta_el * ap.tomo_eta_extra)
    vt_fine = H.var_theta(1)
    n_det = float(np.nanmean(vt_fine["var"] + vt_fine["mean"] ** 2)) - 0.5
    N = T.auto_cutoff(n_det / eta, hi=ap.tomo_nmax)
    occ = H.counts.sum(0) > 0
    xmax = float(np.clip(np.max(np.abs(-H.xmax + (np.flatnonzero(occ) + 0.5) * H.dx)) + 0.3, 4.5, 8.0)) if occ.any() else 6.0
    fth = max(1, 96 // ap.tomo_theta_bins)
    fx = max(1, int(round(ap.tomo_dx / H.dx)))
    counts, te, xe = H.coarse(fth, fx, xmax)
    tm = T.Tomography(counts, te, xe, eta, N)
    r = tm.fit(max_iter=ap.tomo_max_iter)
    met = T.state_metrics(r["rho"])
    if met["tail"] > 1e-3 and N < ap.tomo_nmax:
        N = min(ap.tomo_nmax, N + 6)
        tm = T.Tomography(counts, te, xe, eta, N)
        r = tm.fit(max_iter=ap.tomo_max_iter)
        met = T.state_metrics(r["rho"])
    if met["tail"] > 1e-3:
        warnings.append(f"Fock-Abschneidegrenze N = {N}: Besetzung der obersten Niveaus {met['tail']:.1e}")
    rho = r["rho"]
    # Sollzustand und Verlustmodell
    target = model = None
    fid = {}
    if state and state.get("preset"):
        from ..sim.states import loss_channel, make_state
        try:
            st = make_state(state["preset"])
            rt = st.rho()
            Nt = max(N, rt.shape[0])
            target = rt[:N, :N] / np.real(np.trace(rt[:N, :N]))
            n_id = float(state.get("mean_n_ideal") or np.real(np.trace(rt @ np.diag(np.arange(rt.shape[0])))))
            xx = xi if (xi is not None and np.isfinite(xi) and xi > 0) else 1.0
            from ..sim.states import background_rho
            bgr = background_rho(state, rt.shape[0])      # Dunkelzählungen: Zustand ohne Herald (Quellmodell)
            n_bg = float(np.real(np.trace(bgr @ np.diag(np.arange(rt.shape[0]))))) if bgr is not None else 0.0
            n_mix = xx * n_id + (1 - xx) * n_bg
            eta_o = float(np.clip(met["mean_n"] / n_mix, 0.05, 1.0)) if n_mix > 0 else 1.0
            sd = float(np.median(eA)) if len(eA) else 0.0      # Dephasierung durch die θ-Unsicherheit
            kk = np.arange(rt.shape[0])
            lm = (loss_channel(rt, eta_o) * np.exp(-0.5 * sd ** 2 * (kk[:, None] - kk[None, :]) ** 2))[:N, :N]
            model = xx * lm
            if bgr is None:
                model[0, 0] += 1 - xx
            else:
                model = model + (1 - xx) * (loss_channel(bgr, eta_o)
                                            * np.exp(-0.5 * sd ** 2 * (kk[:, None] - kk[None, :]) ** 2))[:N, :N]
            model = model / np.real(np.trace(model))
            F_i, phi_i = T.fidelity_max_rotation(rho, target, pure=True)
            F_m, phi_m = T.fidelity_max_rotation(rho, model)
            fid = dict(F_ideal=F_i, F_ideal_rotation_rad=phi_i, F_model=F_m, F_model_rotation_rad=phi_m,
                       model=dict(xi=xx, eta_optical=eta_o, dephasing_rad=sd,
                                  note="ξ·D_σθ(L_η(ρ_soll)) + (1−ξ)·ρ_ohne Herald (SPDC: |0⟩⟨0|), η aus ⟨n⟩, σ_θ = Median der θ-Unsicherheit",
                                  background=((state.get("source") or {}).get("model") or "spdc")))
        except Exception as e:                       # Sollzustand unbekannt: keine Treue
            warnings.append(f"Treue zum Sollzustand nicht berechnet: {e}")
    boot = _boot(tm, rho, counts, ap.n_boot_tomo, rng, target, model, ap.tomo_boot_iter) if ap.n_boot_tomo > 1 else {}
    # Var(x_θ) der Daten, Abdeckung
    vt = H.var_theta(max(1, 96 // ap.var_theta_bins))
    vfit = T.fit_var_theta(vt)
    cov = T.coverage(vt)
    if not cov["ok"]:
        warnings.append(f"Phasenabdeckung lückenhaft: {len(cov['gaps'])} von {len(cov['counts'])} θ-Bins unter 25 % "
                        f"des Mittels (Hub/Rampe prüfen)")
    g = np.linspace(-4.5, 4.5, ap.wigner2d_points)
    W = T.wigner2d(rho, g, g)
    th_c = np.linspace(0, 2 * np.pi, 121)
    mu_r, var_r = T.var_curve(rho, th_c)
    out = dict(available=True, source="ramp", label="Phase bekannt (Piezo-Rampe, kalibriert)",
               n_records=H.n, n_outside=H.n_outside, eta_tomo=eta, N=N, n_iter=r["n_iter"], loglik=r["loglik"],
               bins=dict(n_theta=len(te) - 1, n_x=len(xe) - 1, xmax=xmax),
               theta_err_A=dict(median=float(np.median(eA)) if len(eA) else None,
                                p90=float(np.percentile(eA, 90)) if len(eA) else None),
               calibration=_tolist(pc.to_dict()), metrics=met, fidelity=fid, boot=boot,
               var_theta=dict(theta=vt["theta"].tolist(), var=vt["var"].tolist(), var_err=vt["var_err"].tolist(),
                              mean=vt["mean"].tolist(), mean_err=vt["mean_err"].tolist(), n=vt["n"].tolist(),
                              fit=vfit, rho_theta=th_c.tolist(), rho_var=(var_r * eta + 0.5 * (1 - eta)).tolist(),
                              rho_mean=(mu_r * np.sqrt(eta)).tolist()),
               coverage=cov, wigner2d=dict(x=g.tolist(), W=np.round(W, 5).tolist()),
               rho=dict(re=np.round(np.real(rho), 6).tolist(), im=np.round(np.imag(rho), 6).tolist()),
               warnings=warnings, file="phase.h5" if ph_path else None)
    out["B"] = phase_group_B(HB, err_B, eta, ap, rng)
    return out


def phase_group_B(HB, err_B, eta: float, ap, rng) -> dict:
    """Lauf B (ohne Herald) phasenaufgelöst: Var(x_θ)-Fit am Detektor und MaxLik-ρ (gleiche K-θ-Kalibrierung wie A)."""
    if HB.n < 300:
        return dict(available=False, reason=f"zu wenige B-Records mit Phase ({HB.n})", n_records=int(HB.n))
    eB = np.concatenate(err_B) if err_B else np.empty(0)
    vt = HB.var_theta(max(1, 96 // ap.var_theta_bins))
    vfit = T.fit_var_theta(vt)
    vt1 = HB.var_theta(1)
    n_det = float(np.nanmean(vt1["var"] + vt1["mean"] ** 2)) - 0.5
    N = T.auto_cutoff(max(n_det, 0.0) / eta, hi=ap.tomo_nmax)
    occ = HB.counts.sum(0) > 0
    xmax = float(np.clip(np.max(np.abs(-HB.xmax + (np.flatnonzero(occ) + 0.5) * HB.dx)) + 0.3, 4.5, 8.0)) if occ.any() else 6.0
    counts, te, xe = HB.coarse(max(1, 96 // ap.tomo_theta_bins), max(1, int(round(ap.tomo_dx / HB.dx))), xmax)
    tm = T.Tomography(counts, te, xe, eta, N)
    r = tm.fit(max_iter=ap.tomo_max_iter)
    met = T.state_metrics(r["rho"])
    nb = max(0, int(ap.n_boot_tomo) // 2)
    boot = _boot(tm, r["rho"], counts, nb, rng, None, None, ap.tomo_boot_iter) if nb > 1 else {}
    for k in ("F_ideal", "F_model"):
        boot.pop(k, None)
    th_c = np.linspace(0, 2 * np.pi, 121)
    mu_r, var_r = T.var_curve(r["rho"], th_c)
    g = np.linspace(-4.5, 4.5, 41)
    return dict(available=True, n_records=int(HB.n), n_outside=int(HB.n_outside), N=N, eta_tomo=eta,
                n_iter=r["n_iter"], metrics=met, boot=boot,
                theta_err=dict(median=float(np.median(eB)) if len(eB) else None),
                var_theta=dict(theta=vt["theta"].tolist(), var=vt["var"].tolist(), var_err=vt["var_err"].tolist(),
                               n=vt["n"].tolist(), fit=vfit, rho_theta=th_c.tolist(),
                               rho_var=(var_r * eta + 0.5 * (1 - eta)).tolist()),
                wigner2d=dict(x=g.tolist(), W=np.round(T.wigner2d(r["rho"], g, g), 5).tolist()),
                note="Zustand ohne Herald (Lauf B), Quetschung aus Var(x_θ) am Detektor und aus ρ (η_el korrigiert)")


VAR_TESTS = ("A-Läufe: Varianz", "A-B-A: A-Varianz", "Nulltest AA")


def annotate_warnings(warnings: list, phase: dict, state: dict | None) -> list:
    """Varianz-Nulltests der phasengemittelten Auswertung kennzeichnen: Bei Piezo-Rampe und phasenempfindlichem Zustand
    hängt die gepoolte Varianz eines Laufs von seiner θ-Abdeckung ab (Hub ≠ ganzzahlige Wellenlängen, Drift)."""
    if not (phase or {}).get("available") or not (state or {}).get("phase_sensitive"):
        return warnings
    note = " – erwartet bei Piezo-Rampe (θ-Abdeckung je Lauf ungleich), maßgeblich ist die phasenaufgelöste Auswertung"
    return [w + note if isinstance(w, str) and w.startswith(VAR_TESTS) else w for w in warnings]


def _tolist(o):
    if isinstance(o, dict):
        return {k: _tolist(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_tolist(v) for v in o]
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, np.generic):
        return o.item()
    return o
