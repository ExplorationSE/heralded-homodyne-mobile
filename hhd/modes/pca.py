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


def window_sums(data, st, nb, B, offset):
    """Gebinnte Fenster (Rohsummen je Bin, LSB, ohne Skalierung) ab Startsample ``st``; nur vollständige Fenster.

    Liefert (Σw, Σwwᵀ, n) – additiv über Blöcke, daher für die Datenstrom-Auswertung geeignet."""
    L = data.shape[1]
    ok = (st >= 0) & (st + nb * B <= L)
    if not ok.any():
        return np.zeros(nb), np.zeros((nb, nb)), 0
    cols = st[ok, None] + np.arange(nb * B)[None, :]
    w = np.take_along_axis(np.asarray(data[ok], dtype=np.float64), cols, axis=1)
    w = (w - offset).reshape(ok.sum(), nb, B).sum(-1)
    return w.sum(0), w.T @ w, int(len(w))


def window_start(centre_D, first_D, fs, half):
    return np.rint((centre_D - first_D) * fs).astype(np.int64) - half


def pca_geometry(spec: ModeSpec, half_ns: float = 320.0, bin_samples: int = 8):
    """(half, nb): halbe Fensterlänge in Samples (Vielfaches von ``bin_samples``) und Zahl der Bins."""
    half = int(round(half_ns * 1e-9 * spec.fs / bin_samples)) * bin_samples
    return half, (2 * half) // bin_samples


def _accumulate(run, idx, centre_D, half, B, offset, chunk=1024):
    """Summe und Summe der äußeren Produkte der gebinnten Fenster (Rohsummen)."""
    nb = (2 * half) // B
    s1 = np.zeros(nb); s2 = np.zeros((nb, nb)); n = 0
    first = run.first_sample_D()
    idx = np.sort(idx)
    for s in range(0, len(idx), chunk):
        ii = idx[s:s + chunk]
        data = run.read_records(ii)
        a, b, k = window_sums(data, window_start(centre_D[ii], first[ii], run.fs, half), nb, B, offset)
        s1 += a; s2 += b; n += k
    return s1, s2, n


def pca_mode(groups_A, groups_V, offset: float, var_kv_sample: float, spec: ModeSpec, half_ns: float = 320.0,
             bin_samples: int = 8, max_records: int = 20000) -> dict:
    """``groups_*``: Liste (run, idx, centre_D). ``var_kv_sample``: Vakuumvarianz je Sample (LSB²).

    ``max_records`` ≤ 0: alle Records."""
    half, nb = pca_geometry(spec, half_ns, bin_samples)
    cap = max_records if max_records and max_records > 0 else np.inf

    def acc(groups):
        S1 = np.zeros(nb); S2 = np.zeros((nb, nb)); N = 0
        for run, idx, c in groups or []:
            if N >= cap:
                break
            if np.isfinite(cap):
                idx = idx[: max(0, int(cap - N))]
            s1, s2, n = _accumulate(run, idx, c, half, bin_samples, offset)
            S1 += s1; S2 += s2; N += n
        return S1, S2, N

    return pca_from_sums(acc(groups_A), acc(groups_V), var_kv_sample, spec, half_ns, bin_samples)


def pca_from_sums(sums_A, sums_V, var_kv_sample: float, spec: ModeSpec, half_ns: float = 320.0,
                  bin_samples: int = 8) -> dict:
    """PCA aus additiven Fenstersummen (Σw, Σwwᵀ, n) für A und K-V (Rohsummen, LSB, Offset abgezogen).

    Gemeinsamer Kern der In-Memory- und der Datenstrom-Auswertung (inkrementelle Kovarianz)."""
    fs = spec.fs
    norm = var_kv_sample / 0.5 * bin_samples              # → Var ≈ ½ je Bin im Vakuum

    def cov(S):
        s1, s2, n = S
        if n == 0:
            return None, 0
        m = s1 / n
        return (s2 / n - np.outer(m, m)) / norm, int(n)

    CA, nA = cov(sums_A)
    CV, nV = cov(sums_V)
    if nA < 100 or nV < 100:
        return dict(available=False, reason=f"zu wenige vollständige Fenster (A {nA}, K-V {nV}; Fenster ±{half_ns} ns)",
                    n_A=int(nA), n_V=int(nV))
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
    return dict(available=True, n_A=int(nA), n_V=int(nV), bins=int(nb), bin_samples=bin_samples, half_ns=half_ns,
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
