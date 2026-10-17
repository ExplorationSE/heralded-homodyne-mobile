"""Feinkalibrierung der Verzögerung Δ über das Maximum der bedingten Quadraturvarianz (Vorbericht 2, DELTA_SCAN).

Für jede Verzögerung δ wird die Quadratur der Mode an der Stelle „Trigger + δ" aus allen zugeordneten,
ungestörten Records extrahiert; die Varianz ⟨Q²⟩ ist maximal, wenn die Mode auf dem heraldierten Zustand liegt:

    V(δ) = c + A·O(δ − Δ_T)²,  O(δ) = (1 + γ|δ|)·e^(−γ|δ|)   (doppelt-exponentielle Mode)

Die Skala kommt aus Vakuumläufen (K-V: Varianz 1/2), ersatzweise aus dem frühen Pretrigger-Bereich.

Δ_T (Mode − Trigger) ist eine Eigenschaft der Verkabelung und daher sitzungsweit gepoolt; die Verzögerung relativ
zum abgebildeten Herald folgt je Lauf als Δ = Δ_T + δ_HT (δ_HT = Trigger − abgebildeter Herald), weil die
Quantisierung der SYNC-Zeitstempel (3,2 ns) die Phase der Abbildung je Lauf verschiebt.

Speicher: Records werden blockweise gelesen; gespeichert wird nur Q² an den Rasterpunkten (n_rec × n_grid).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import curve_fit


def mode_kernel(shape: str, gamma: float, fs: float) -> np.ndarray:
    if shape == "gauss":
        half = int(np.ceil(6.0 / gamma * fs))
        t = np.arange(-half, half + 1) / fs
        k = np.exp(-0.5 * (t * gamma) ** 2)
    else:
        half = int(np.ceil(10.0 / gamma * fs))
        t = np.arange(-half, half + 1) / fs
        k = np.exp(-gamma * np.abs(t))
    return k / np.sqrt(np.sum(k ** 2))


def overlap(delta: np.ndarray, gamma: float, shape: str = "double_exponential") -> np.ndarray:
    if shape == "gauss":
        return np.exp(-0.25 * (np.asarray(delta) * gamma) ** 2)
    d = np.abs(delta) * gamma
    return (1 + d) * np.exp(-d)


def correlate(y: np.ndarray, kern: np.ndarray) -> np.ndarray:
    """Q[i, n] = Σ_k y_i[k]·f(k − n) (Mode zentriert auf Sample n), FFT."""
    L = y.shape[1]; Kn = len(kern)
    nfft = 1 << int(np.ceil(np.log2(L + Kn)))
    full = np.fft.irfft(np.fft.rfft(y, nfft, axis=1) * np.fft.rfft(kern[::-1], nfft), nfft, axis=1)
    h = (Kn - 1) // 2
    return full[:, h:h + L]


def interp_rows(Q: np.ndarray, pos: np.ndarray) -> np.ndarray:
    """Q an fraktionalen Positionen pos[i, g] (Zeile i); außerhalb → NaN."""
    L = Q.shape[1]
    bad = (pos < 0) | (pos > L - 1)
    p = np.clip(pos, 0, L - 1.000001)
    i = np.floor(p).astype(np.int64); fr = p - i
    r = np.arange(Q.shape[0])[:, None]
    v = Q[r, i] * (1 - fr) + Q[r, i + 1] * fr
    v[bad] = np.nan
    return v


@dataclass
class VacuumNorm:
    offset: float           # ADC-Nullpunkt (LSB)
    scale: float            # LSB pro Vakuum-Quadratureinheit (Var = 1/2)
    n_records: int
    source: str


def vacuum_norm(records_iter, kern: np.ndarray, source: str = "K-V", stride: int = 50,
                edge: int | None = None) -> VacuumNorm | None:
    """Mittelwert und Skala aus Vakuumrecords (Iterator über int16-Blöcke)."""
    s1 = s2 = 0.0; n = 0; qs = []
    for blk in records_iter:
        s1 += float(np.sum(blk, dtype=np.float64)); s2 += blk.size; n += len(blk)
        qs.append(blk)
    if n == 0:
        return None
    off = s1 / s2
    acc = 0.0; m = 0
    for blk in qs:
        Q = correlate(blk.astype(np.float64) - off, kern)
        L = Q.shape[1]
        e = edge if edge is not None else len(kern) // 2
        c = Q[:, e:L - e:stride] if L - 2 * e > stride else Q[:, L // 4:3 * L // 4:stride]
        acc += np.sum(c ** 2); m += c.size
    return VacuumNorm(float(off), float(np.sqrt(acc / m / 0.5)), n, source)


def collect_q2(read_block, idx: np.ndarray, pos0: np.ndarray, fs: float, grid: np.ndarray, kern: np.ndarray,
               norm: VacuumNorm, chunk: int = 512) -> np.ndarray:
    """Q² (n_rec × n_grid). ``read_block(idx)`` liefert int16-Records; ``pos0``: Sampleposition der Referenz
    (Trigger) im Record; grid: Verzögerungen in s."""
    out = np.empty((len(idx), len(grid)))
    for s in range(0, len(idx), chunk):
        sl = slice(s, s + chunk)
        y = (read_block(idx[sl]).astype(np.float64) - norm.offset) / norm.scale
        Q = correlate(y, kern)
        out[sl] = interp_rows(Q, pos0[sl, None] + grid[None, :] * fs) ** 2
    return out


def col_mean(Q: np.ndarray) -> np.ndarray:
    """Spaltenmittel ohne NaN; Spalten ganz ohne Werte (Rasterpunkt außerhalb aller Records) → NaN, ohne
    ``RuntimeWarning: Mean of empty slice``."""
    ok = np.isfinite(Q)
    n = ok.sum(axis=0)
    s = np.where(ok, Q, 0.0).sum(axis=0)
    out = np.full(Q.shape[1] if Q.ndim > 1 else 0, np.nan)
    np.divide(s, n, out=out, where=n > 0)
    return out


def _fit(grid, V, gamma, shape, p0=None):
    model = lambda d, c, A, d0: c + A * overlap(d - d0, gamma, shape) ** 2
    ok = np.isfinite(V)
    if p0 is None:
        p0 = [np.nanmedian(V), np.nanmax(V) - np.nanmedian(V), grid[np.nanargmax(V)]]
    popt, pcov = curve_fit(model, grid[ok], V[ok], p0=p0, maxfev=4000)
    return popt, np.sqrt(np.abs(np.diag(pcov))), model


def scan(Q2: np.ndarray, grid: np.ndarray, gamma: float, shape: str, n_boot: int = 100, seed: int = 1) -> dict:
    V = col_mean(Q2)
    popt, perr, _ = _fit(grid, V, gamma, shape)
    rng = np.random.default_rng(seed)
    bs = []
    for _ in range(n_boot):
        ii = rng.integers(0, len(Q2), len(Q2))
        try:
            p, _, _ = _fit(grid, col_mean(Q2[ii]), gamma, shape, p0=popt)
            bs.append(p)
        except (RuntimeError, ValueError):
            pass
    bs = np.array(bs) if bs else np.full((1, 3), np.nan)
    c, A, d0 = popt
    return dict(V=V, grid=grid, c=float(c), A=float(A), delta=float(d0), sigma_fit=float(perr[2]),
                sigma_boot=float(np.nanstd(bs[:, 2])) if len(bs) > 1 else float(perr[2]),
                sigma_A=float(np.nanstd(bs[:, 1])) if len(bs) > 1 else float(perr[1]), n_records=int(len(Q2)),
                n_boot=int(len(bs)), contrast=float(A / c) if c else float("nan"))


def calibrate(sources: list, fs: float, kern: np.ndarray, norm: VacuumNorm, gamma: float, shape: str,
              coarse=(-700e-9, 1100e-9, 8e-9), fine_half=120e-9, fine_step=1e-9, n_boot=100, seed=1) -> dict:
    """``sources``: Liste (read_block, idx, pos0) je Lauf (pos0 = Triggerposition im Record in Samples).
    Ergebnis: Δ_T (Mode − Trigger) mit Bootstrap-Unsicherheit."""
    def q2(grid):
        return np.concatenate([collect_q2(rb, ix, p0, fs, grid, kern, norm) for rb, ix, p0 in sources if len(ix)])
    g1 = np.arange(coarse[0], coarse[1] + coarse[2] / 2, coarse[2])
    Q1 = q2(g1)
    V1 = col_mean(Q1)
    if not np.isfinite(V1).any():
        raise ValueError("Δ-Grobraster liegt ganz außerhalb der Records")
    d_coarse = float(g1[np.nanargmax(V1)])
    g2 = np.arange(d_coarse - fine_half, d_coarse + fine_half + fine_step / 2, fine_step)
    res = scan(q2(g2), g2, gamma, shape, n_boot=n_boot, seed=seed)
    res["coarse"] = dict(grid=g1, V=V1, peak=d_coarse)
    return res
