"""Datengetriebene Modenbestimmung: Eigenmoden der bedingten Kovarianz (Prüfung bzw. Option).

Fenster ±``half_ns`` um das Modenzentrum, in Bins zu ``bin`` Samples gemittelt (Rauschen je Bin
unabhängig genug, Dimension ~100). Kovarianz der sauberen Herald-Records C_A minus Kovarianz der K-V-Fenster C_V
(beide in Vakuumeinheiten): ΔC = C_A − C_V hat für einen Einzelmodenzustand Rang 1 mit Eigenvektor ∝ Mode und
Eigenwert ⟨x²⟩_A − ½ (≈ η_eff für |1⟩). Ausgewiesen: Überlapp |⟨e₁, f⟩|² mit der parametrischen Mode,
Eigenwertverhältnis λ₂/λ₁ (Mehrmodigkeit), angepasstes γ, Zentrumsversatz.
"""
from __future__ import annotations

import numpy as np

from .extract import ModeSpec, mode_function


def _accumulate(run, idx, centre_D, half, B, offset, scale, chunk=1024):
    """Summe und Summe der äußeren Produkte der gebinnten Fenster."""
    nb = (2 * half) // B
    s1 = np.zeros(nb); s2 = np.zeros((nb, nb)); n = 0
    first = run.first_sample_D()
    L = run.record_length
    idx = np.sort(idx)
    for s in range(0, len(idx), chunk):
        ii = idx[s:s + chunk]
        data = run.read_records(ii).astype(np.float64)
        st = np.rint((centre_D[ii] - first[ii]) * run.fs).astype(np.int64) - half
        ok = (st >= 0) & (st + nb * B <= L)
        if not ok.any():
            continue
        cols = st[ok, None] + np.arange(nb * B)[None, :]
        w = np.take_along_axis(data[ok], cols, axis=1)
        w = (w - offset).reshape(ok.sum(), nb, B).sum(-1) / (scale * np.sqrt(B))   # Var ≈ ½ je Bin im Vakuum
        s1 += w.sum(0); s2 += w.T @ w; n += len(w)
    return s1, s2, n


def pca_mode(groups_A, groups_V, offset: float, var_kv_sample: float, spec: ModeSpec, half_ns: float = 320.0,
             bin_samples: int = 8, max_records: int = 20000) -> dict:
    """``groups_*``: Liste (run, idx, centre_D). ``var_kv_sample``: Vakuumvarianz je Sample (LSB²)."""
    fs = spec.fs
    half = int(round(half_ns * 1e-9 * fs / bin_samples)) * bin_samples
    scale = np.sqrt(var_kv_sample / 0.5)

    def acc(groups):
        S1 = S2 = None; N = 0
        for run, idx, c in groups:
            if N >= max_records:
                break
            idx = idx[: max(0, max_records - N)]
            s1, s2, n = _accumulate(run, idx, c, half, bin_samples, offset, scale)
            S1 = s1 if S1 is None else S1 + s1; S2 = s2 if S2 is None else S2 + s2; N += n
        m = S1 / N
        return S2 / N - np.outer(m, m), N

    CA, nA = acc(groups_A)
    CV, nV = acc(groups_V)
    dC = CA - CV
    w, V = np.linalg.eigh(dC)
    o = np.argsort(w)[::-1]; w, V = w[o], V[:, o]
    e1 = V[:, 0]
    nb = len(e1)
    k = np.arange(nb * bin_samples) - (nb * bin_samples - 1) / 2                 # Sample-Raster (relativ)

    def binned(sp, shift=0.0):
        ff = mode_function(sp, k - shift).reshape(nb, bin_samples).sum(1)
        return ff / np.linalg.norm(ff)

    f = binned(spec)
    if e1 @ f < 0:
        e1 = -e1
    ov = float((e1 @ f) ** 2)
    # γ-Anpassung und Zentrumsversatz (Gitter)
    best = (np.inf, spec.gamma, 0.0)
    for g in spec.gamma * np.linspace(0.5, 2.0, 61):
        sp = ModeSpec(spec.shape, g, fs)
        for sh in np.linspace(-2, 2, 41) * bin_samples:
            r = float(np.sum((e1 - binned(sp, sh)) ** 2))
            if r < best[0]:
                best = (r, g, sh)
    # erwartete Überlappminderung allein durch statistisches Rauschen (Störungsrechnung, Wishart-Näherung)
    l1 = w[0]
    a1, v1 = e1 @ CA @ e1, e1 @ CV @ e1
    noise = sum((V[:, j] @ CA @ V[:, j]) * a1 / nA + (V[:, j] @ CV @ V[:, j]) * v1 / nV for j in range(1, nb))
    d_noise = float(noise / l1 ** 2) if l1 > 0 else float("nan")
    # Rauschgrenze des Überlapps (Monte-Carlo-frei: Eigenvektorfehler ~ √(nb/n)·σ/λ₁)
    return dict(n_A=int(nA), n_V=int(nV), bins=int(nb), bin_samples=bin_samples, half_ns=half_ns,
                units="Vakuumvarianz je Sample ≙ ½ (Bins korreliert)", eigenvalues=w[:5].tolist(), lambda1=float(w[0]), ratio_l2_l1=float(w[1] / w[0]) if w[0] > 0 else float("nan"),
                overlap_param=ov, overlap_noise_expected=d_noise,
                overlap_param_corrected=float(min(1.0, ov + d_noise)), eta_mode_mismatch=float(max(0.0, 1 - ov - d_noise)), gamma_fit=float(best[1]), tau_fit_ns=float(1e9 / best[1]),
                centre_shift_ns=float(best[2] / fs * 1e9), mode=e1.tolist(), mode_param=f.tolist())


def pca_fitted_template(pca: dict, spec: ModeSpec) -> ModeSpec:
    """Parametrische Mode mit γ und Zentrumsversatz aus der PCA-Anpassung (Option ``mode_source = "pca"``).

    Zwei angepasste Parameter statt ~100 freier Bins: keine nennenswerte Auswahlverzerrung der Varianz."""
    sp = ModeSpec(spec.shape, pca["gamma_fit"], spec.fs, spec.bandwidth_Hz)
    h = sp.half + int(np.ceil(abs(pca["centre_shift_ns"]) * 1e-9 * spec.fs)) + 1
    k = np.arange(-h, h + 1)
    f = mode_function(sp, k - pca["centre_shift_ns"] * 1e-9 * spec.fs)
    return ModeSpec("custom", pca["gamma_fit"], spec.fs, spec.bandwidth_Hz, custom=f / np.linalg.norm(f))


def pca_template(pca: dict, spec: ModeSpec) -> ModeSpec:
    """Rohe Eigenmode als Vorlage (``mode_source = "pca_raw"``, nur zur Prüfung): an denselben Daten bestimmt,
    daher Auswahlverzerrung nach oben (Varianz um etwa den erwarteten Rauschanteil des Überlapps × λ₁ zu groß).

    Datengetriebene Mode als Vorlage (lineare Interpolation auf das Sample-Raster, Ränder → 0)."""
    e1 = np.asarray(pca["mode"]); B = pca["bin_samples"]
    nb = len(e1)
    u = (np.arange(nb) - (nb - 1) / 2) * B
    h = int(nb * B / 2)
    k = np.arange(-h, h + 1)
    f = np.interp(k, u, e1, left=0.0, right=0.0)
    return ModeSpec("custom", spec.gamma, spec.fs, spec.bandwidth_Hz, custom=f / np.linalg.norm(f))
