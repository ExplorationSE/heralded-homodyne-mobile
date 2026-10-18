"""Feinkalibrierung der Verzögerung Δ über das Maximum der bedingten Quadraturvarianz (Vorbericht 2, DELTA_SCAN).

Für jede Verzögerung δ wird die Quadratur der Mode an der Stelle „Trigger + δ" aus allen zugeordneten,
ungestörten Records extrahiert; die Varianz ⟨Q²⟩ ist maximal, wenn die Mode auf dem heraldierten Zustand liegt:

    V(δ) = c + A·O(δ − Δ_T)²,  O(δ) = (1 + γ|δ|)·e^(−γ|δ|)   (doppelt-exponentielle Mode)

Die Skala kommt aus Vakuumläufen (K-V: Varianz 1/2), ersatzweise aus dem frühen Pretrigger-Bereich.

Δ_T (Mode − Trigger) ist eine Eigenschaft der Verkabelung und daher sitzungsweit gepoolt; die Verzögerung relativ
zum abgebildeten Herald folgt je Lauf als Δ = Δ_T + δ_HT (δ_HT = Trigger − abgebildeter Herald), weil die
Quantisierung der SYNC-Zeitstempel (3,2 ns) die Phase der Abbildung je Lauf verschiebt.

Speicher (ab 0.13): Records werden blockweise gelesen und Q² an den Rasterpunkten sofort in Summen je Rasterpunkt
akkumuliert (``GridAccumulator``); die Bootstrap-Unsicherheit kommt aus einem Poisson-Bootstrap (Gewicht
w ~ Poisson(1) je Record und Replikat), der ebenfalls nur Summen (n_boot × n_grid) hält. Der Speicher hängt damit
nicht mehr von der Zahl der Kandidaten ab. Die frühere Matrixvariante (n_rec × n_grid, Multinomial-Bootstrap)
bleibt als ``method="matrix"`` zum Vergleich erhalten.
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


def chunk_rows(record_length: int, n_kern: int, work_bytes: float = 24e6, max_rows: int = 512) -> int:
    """Records je FFT-Block, so dass der Arbeitsspeicher (float64-Zeitreihe + komplexes Spektrum) etwa
    ``work_bytes`` bleibt (Ergebnis unabhängig von der Blockung)."""
    nfft = 1 << int(np.ceil(np.log2(record_length + n_kern)))
    return int(max(8, min(max_rows, work_bytes // (nfft * 8 * 4))))


def iter_q2(read_block, idx: np.ndarray, pos0: np.ndarray, fs: float, grid: np.ndarray, kern: np.ndarray,
            norm: VacuumNorm, chunk: int | None = None):
    """Q²-Blöcke (m × n_grid) in Record-Reihenfolge. ``read_block(idx)`` liefert int16-Records; ``pos0``:
    Sampleposition der Referenz (Trigger) im Record; grid: Verzögerungen in s."""
    s = 0
    while s < len(idx):
        if chunk is None:                                # Blockgröße nach Recordlänge (erster Block: 8 Records)
            y = read_block(idx[s:s + 8]); c = chunk_rows(y.shape[1], len(kern))
            if c > 8 and s + 8 < len(idx):
                y = np.concatenate([y, read_block(idx[s + 8:s + c])])
            chunk = c
        else:
            y = read_block(idx[s:s + chunk])
        m = len(y)
        y = (y.astype(np.float64) - norm.offset) / norm.scale
        Q = correlate(y, kern)
        yield interp_rows(Q, pos0[s:s + m, None] + grid[None, :] * fs) ** 2
        s += m


def collect_q2(read_block, idx, pos0, fs, grid, kern, norm, chunk: int | None = None) -> np.ndarray:
    """Q² als Matrix (n_rec × n_grid); nur für die Vergleichsvariante ``method="matrix"``."""
    out = np.empty((len(idx), len(grid))); s = 0
    for q in iter_q2(read_block, idx, pos0, fs, grid, kern, norm, chunk):
        out[s:s + len(q)] = q; s += len(q)
    return out


class GridAccumulator:
    """Summen von Q² je Rasterpunkt (NaN = außerhalb des Records, ausgelassen) plus Poisson-Bootstrap.

    Die Bootstrap-Gewichte werden recordweise (Zeilen m × n_boot) aus einem Generator gezogen; das Ergebnis hängt
    daher nur von der Record-Reihenfolge ab, nicht von der Blockung. Speicher O(n_boot · n_grid)."""

    def __init__(self, n_grid: int, n_boot: int = 0, seed: int = 1):
        self.S = np.zeros(n_grid); self.N = np.zeros(n_grid)
        self.n_boot = int(n_boot)
        self.Sb = np.zeros((self.n_boot, n_grid)); self.Nb = np.zeros((self.n_boot, n_grid))
        self.rng = np.random.default_rng(seed)
        self.n = 0

    def add(self, q2: np.ndarray) -> None:
        ok = np.isfinite(q2)
        z = np.where(ok, q2, 0.0)
        self.S += z.sum(axis=0); self.N += ok.sum(axis=0)
        if self.n_boot:
            W = self.rng.poisson(1.0, (len(q2), self.n_boot)).astype(np.float64)
            self.Sb += W.T @ z; self.Nb += W.T @ ok
        self.n += len(q2)

    @staticmethod
    def _mean(S, N):
        out = np.full(S.shape, np.nan)
        np.divide(S, N, out=out, where=N > 0)
        return out

    def mean(self) -> np.ndarray:
        return self._mean(self.S, self.N)

    def boot_means(self) -> np.ndarray:
        return self._mean(self.Sb, self.Nb)


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


def _scan_result(V, boot_V, grid, gamma, shape, n_records) -> dict:
    popt, perr, _ = _fit(grid, V, gamma, shape)
    bs = []
    for Vb in boot_V:
        try:
            p, _, _ = _fit(grid, Vb, gamma, shape, p0=popt)
            bs.append(p)
        except (RuntimeError, ValueError, TypeError):
            pass
    bs = np.array(bs) if bs else np.full((1, 3), np.nan)
    c, A, d0 = popt
    return dict(V=V, grid=grid, c=float(c), A=float(A), delta=float(d0), sigma_fit=float(perr[2]),
                sigma_boot=float(np.nanstd(bs[:, 2])) if len(bs) > 1 else float(perr[2]),
                sigma_A=float(np.nanstd(bs[:, 1])) if len(bs) > 1 else float(perr[1]), n_records=int(n_records),
                n_boot=int(len(bs)), contrast=float(A / c) if c else float("nan"))


def scan(Q2: np.ndarray, grid: np.ndarray, gamma: float, shape: str, n_boot: int = 100, seed: int = 1) -> dict:
    """Matrixvariante: Multinomial-Bootstrap über die Zeilen von Q2."""
    rng = np.random.default_rng(seed)
    boot = (col_mean(Q2[rng.integers(0, len(Q2), len(Q2))]) for _ in range(n_boot))
    res = _scan_result(col_mean(Q2), boot, grid, gamma, shape, len(Q2))
    res["bootstrap"] = "multinomial"
    return res


def scan_acc(acc: GridAccumulator, grid: np.ndarray, gamma: float, shape: str) -> dict:
    """Streamingvariante aus den akkumulierten Summen (Poisson-Bootstrap)."""
    res = _scan_result(acc.mean(), acc.boot_means(), grid, gamma, shape, acc.n)
    res["bootstrap"] = "poisson"
    return res


def calibrate(sources: list, fs: float, kern: np.ndarray, norm: VacuumNorm, gamma: float, shape: str,
              coarse=(-700e-9, 1100e-9, 8e-9), fine_half=120e-9, fine_step=1e-9, n_boot=100, seed=1,
              method: str = "stream", chunk: int | None = None) -> dict:
    """``sources``: Liste (read_block, idx, pos0) je Lauf (pos0 = Triggerposition im Record in Samples).
    Ergebnis: Δ_T (Mode − Trigger) mit Bootstrap-Unsicherheit.

    method = "stream" (Standard): Summen je Rasterpunkt, Poisson-Bootstrap, Speicher unabhängig von der
    Kandidatenzahl; "matrix": frühere Variante mit Q²-Matrix und Multinomial-Bootstrap (Vergleich)."""
    if method not in ("stream", "matrix"):
        raise ValueError(f"unbekannte Δ-Methode {method!r}")

    def blocks(grid):
        for rb, ix, p0 in sources:
            if len(ix):
                yield from iter_q2(rb, ix, p0, fs, grid, kern, norm, chunk)

    def accumulate(grid, nb):
        acc = GridAccumulator(len(grid), nb, seed)
        for q in blocks(grid):
            acc.add(q)
        return acc

    g1 = np.arange(coarse[0], coarse[1] + coarse[2] / 2, coarse[2])
    if method == "matrix":
        V1 = col_mean(np.concatenate(list(blocks(g1))))
    else:
        V1 = accumulate(g1, 0).mean()
    if not np.isfinite(V1).any():
        raise ValueError("Δ-Grobraster liegt ganz außerhalb der Records")
    d_coarse = float(g1[np.nanargmax(V1)])
    g2 = np.arange(d_coarse - fine_half, d_coarse + fine_half + fine_step / 2, fine_step)
    if method == "matrix":
        res = scan(np.concatenate(list(blocks(g2))), g2, gamma, shape, n_boot=n_boot, seed=seed)
    else:
        res = scan_acc(accumulate(g2, n_boot), g2, gamma, shape)
    res["coarse"] = dict(grid=g1, V=V1, peak=d_coarse)
    res["method"] = method
    return res
