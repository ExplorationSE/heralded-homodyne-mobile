"""Numerische Kerne der Synchronisation (NumPy-Referenz, AP4).

**Schnittstelle für den Rust-Kern (AP9):** Jede Funktion hier ist rein (keine Datei-, Konfigurations- oder
Objektzugriffe), nimmt nur 1-D-Arrays aus ``float64``/``int64`` und Skalare und gibt Arrays bzw. ein einfaches
``dict`` zurück. Ein späterer Rust-Kern (PyO3) implementiert dieselben Signaturen; ``hhd.sync.backend()``
wählt die Implementierung (``HHD_SYNC_BACKEND=python|rust``). Die Gleichheitstests in ``tests/test_sync.py``
gelten dann für beide.

Zeiten in Sekunden (float64). Bei 1 h Laufdauer beträgt die Auflösung von float64 ≈ 0,5 ps – genug für
Harp (1 ps) und ADQ32 (50 ps).
"""
from __future__ import annotations

import numpy as np
from scipy.linalg import solveh_banded

__all__ = ["grid_index", "xcorr_shift", "pair_by_index", "robust_linear", "piecewise_fit", "piecewise_eval",
           "nearest_diff", "count_in_window", "window_index", "robust_center"]


def robust_center(x: np.ndarray, n_iter: int = 4, k: float = 6.0, floor: float = 0.0) -> tuple[float, float, np.ndarray]:
    """Median mit iterativem MAD-Schnitt. Rückgabe (Mitte, robuste Streuung, verwendete Maske)."""
    x = np.asarray(x, float)
    if len(x) == 0:
        return float("nan"), float("nan"), np.zeros(0, bool)
    c = float(np.median(x))
    use = np.ones(len(x), bool)
    s = 0.0
    for _ in range(n_iter):
        s = 1.4826 * float(np.median(np.abs(x[use] - c))) if use.any() else 0.0
        new = np.abs(x - c) <= max(k * s, floor)
        if not new.any():
            break
        c = float(np.median(x[new]))
        if np.array_equal(new, use):
            break
        use = new
    return c, s, use


def grid_index(t: np.ndarray, period: float, gate: float = 0.25) -> dict:
    """Gitterindizes für (fast) periodische Marker mit Auslassungen und Störmarkern (Vorbericht 2, GITTERINDEX).

    Periode und Phase werden in wachsenden Abschnitten (2 000, 8 000, … Marker) per Ausgleichsgerade
    nachgeführt, damit sich ein kleiner Fehler der Startperiode über lange Läufe nicht aufsummiert.
    Störmarker mit Gitterabweichung ≥ ``gate``·P werden verworfen; bei doppeltem Index bleibt der Marker
    mit der kleinsten Abweichung. Rückgabe: k (int64), keep (Indizes in t), P, phase, rejected.
    """
    t = np.sort(np.asarray(t, float))
    n = len(t)
    if n < 3:
        return dict(k=np.arange(n, dtype=np.int64), keep=np.arange(n), period=float(period), phase=float(t[0]) if n else 0.0,
                    rejected=0)
    d = np.diff(t)
    near = np.abs(d - period) < period * gate
    P = float(np.median(d[near])) if near.sum() > 10 else float(period)
    m0 = min(n, 2000)
    sub = t[:m0] - t[0]
    ph = t[0] + float(np.angle(np.mean(np.exp(2j * np.pi * sub / P)))) / (2 * np.pi) * P
    m = m0
    while True:
        tt = t[:m]
        k = np.rint((tt - ph) / P)
        r = tt - ph - k * P
        ok = np.abs(r - np.median(r)) < gate * P
        if ok.sum() >= 3:
            kc = k[ok].mean()
            A = np.vstack([np.ones(ok.sum()), k[ok] - kc]).T
            c, *_ = np.linalg.lstsq(A, tt[ok], rcond=None)
            P = float(c[1])
            ph = float(c[0] - c[1] * kc)
        if m == n:
            break
        m = min(n, 4 * m)
    k = np.rint((t - ph) / P).astype(np.int64)
    r = t - ph - k * P
    ok = np.abs(r - np.median(r)) < gate * P
    idx = np.flatnonzero(ok)
    k, r = k[idx], r[idx]
    # doppelte Indizes (Prellen, Störmarker): kleinste Abweichung behalten
    keep = np.ones(len(idx), bool)
    if len(idx) > 1:
        dup = np.flatnonzero(np.diff(k) == 0)
        if len(dup):
            rm = np.median(r)
            for i in dup:
                j = i if abs(r[i] - rm) > abs(r[i + 1] - rm) else i + 1
                keep[j] = False
    return dict(k=k[keep], keep=idx[keep], period=P, phase=ph, rejected=int(n - keep.sum()))


def xcorr_shift(kH: np.ndarray, kD: np.ndarray, max_shift: int | None = None) -> dict:
    """Ganzzahliger Periodenversatz s mit kH = kD + s aus den Belegungsvektoren (+1 Marker, −1 fehlt).

    Kreuzkorrelation per FFT; ``uniqueness`` = zweitgrößter / größter Korrelationswert (≪ 1 eindeutig)."""
    kH = np.asarray(kH, np.int64); kD = np.asarray(kD, np.int64)
    if len(kH) < 2 or len(kD) < 2:
        return dict(shift=0, uniqueness=1.0, peak=0.0, n_overlap=0)
    h0, d0 = int(kH.min()), int(kD.min())
    a, b = kH - h0, kD - d0
    nh, nd = int(a.max()) + 1, int(b.max()) + 1
    n = nh + nd
    nfft = 1 << int(np.ceil(np.log2(n)))
    aH = np.zeros(nfft); aD = np.zeros(nfft)
    aH[:nh] = -1.0; aD[:nd] = -1.0
    aH[a] = 1.0; aD[b] = 1.0
    aH[:nh] -= aH[:nh].mean(); aD[:nd] -= aD[:nd].mean()
    xc = np.fft.irfft(np.fft.rfft(aH) * np.conj(np.fft.rfft(aD)), nfft)
    lag = np.arange(nfft); lag = np.where(lag < nfft // 2, lag, lag - nfft)
    if max_shift is not None:
        xc = np.where(np.abs(lag) <= max_shift, xc, -np.inf)
    i = int(np.argmax(xc))
    best = float(xc[i])
    xs = xc.copy(); xs[i] = -np.inf
    second = float(np.max(xs)) if np.isfinite(xs).any() else 0.0
    u = second / best if best > 0 else 1.0
    s = int(lag[i]) + h0 - d0
    n_ov = int(len(np.intersect1d(kH, kD + s, assume_unique=True)))
    return dict(shift=s, uniqueness=max(0.0, u), peak=best, n_overlap=n_ov)


def pair_by_index(kH: np.ndarray, kD: np.ndarray, shift: int) -> tuple[np.ndarray, np.ndarray]:
    """Indizes (iH, iD) der Paare mit kH = kD + shift."""
    _, iH, iD = np.intersect1d(kH, kD + shift, assume_unique=True, return_indices=True)
    return iH, iD


def robust_linear(x: np.ndarray, y: np.ndarray, clip: float = 5.0, n_iter: int = 8, floor: float = 0.0) -> dict:
    """y = a0 + b·(x − x0), x0 = Mittelwert (unkorrelierte Parameter); iterativer 5σ-Schnitt (σ = 1,4826·MAD).

    Rückgabe: a0 (bei x0), b, x0, cov (2×2, für a0, b), sigma_r, used (Maske), residuals."""
    x = np.asarray(x, float); y = np.asarray(y, float)
    n = len(x)
    use = np.ones(n, bool)
    for _ in range(n_iter):
        x0 = float(x[use].mean())
        X = np.vstack([np.ones(use.sum()), x[use] - x0]).T
        c, *_ = np.linalg.lstsq(X, y[use], rcond=None)
        res = y - (c[0] + c[1] * (x - x0))
        mad = 1.4826 * float(np.median(np.abs(res[use] - np.median(res[use]))))
        new = np.abs(res) <= max(clip * mad, floor, 1e-15)
        if new.sum() < 3 or np.array_equal(new, use):
            break
        use = new
    x0 = float(x[use].mean())
    X = np.vstack([np.ones(use.sum()), x[use] - x0]).T
    c, *_ = np.linalg.lstsq(X, y[use], rcond=None)
    res = y - (c[0] + c[1] * (x - x0))
    dof = max(int(use.sum()) - 2, 1)
    sig = float(np.sqrt(np.sum(res[use] ** 2) / dof))
    cov = sig ** 2 * np.linalg.inv(X.T @ X)
    return dict(a0=float(c[0]), b=float(c[1]), x0=x0, cov=cov, sigma_r=sig, used=use, residuals=res)


def _hat(x: np.ndarray, x_start: float, W: float, n_knots: int) -> tuple[np.ndarray, np.ndarray]:
    u = (x - x_start) / W
    i = np.clip(np.floor(u).astype(np.int64), 0, n_knots - 2)
    return i, np.clip(u - i, 0.0, 1.0)


def piecewise_fit(x: np.ndarray, r: np.ndarray, x_start: float, W: float, n_knots: int,
                  smooth: float = 1e-6) -> np.ndarray:
    """Stetige stückweise lineare Funktion (Hutfunktionen, Knotenabstand W) durch die Punkte (x, r).

    Normalgleichungen sind tridiagonal (jeder Punkt berührt zwei Knoten) → O(n) Speicher und Zeit; eine kleine
    Glättung der ersten Differenzen (``smooth`` · mittleres Diagonalelement) überbrückt Knoten ohne Punkte."""
    i, f = _hat(np.asarray(x, float), x_start, W, n_knots)
    w0, w1 = 1.0 - f, f
    diag = np.bincount(i, w0 * w0, n_knots) + np.bincount(i + 1, w1 * w1, n_knots)
    off = np.bincount(i, w0 * w1, n_knots)[:-1]
    rhs = np.bincount(i, w0 * r, n_knots) + np.bincount(i + 1, w1 * r, n_knots)
    lam = smooth * max(float(diag.mean()), 1e-30)
    dd = np.full(n_knots, 2 * lam); dd[0] = dd[-1] = lam
    diag = diag + dd + 1e-12 * lam
    off = off - lam
    ab = np.zeros((2, n_knots))
    ab[0, 1:] = off
    ab[1] = diag
    return solveh_banded(ab, rhs)


def piecewise_eval(x: np.ndarray, coef: np.ndarray, x_start: float, W: float) -> np.ndarray:
    i, f = _hat(np.asarray(x, float), x_start, W, len(coef))
    return coef[i] * (1.0 - f) + coef[i + 1] * f


def nearest_diff(t: np.ndarray, ref: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Für jedes t: Index des nächsten Elements in ``ref`` (sortiert) und die Differenz t − ref."""
    if len(ref) == 0:
        return np.full(len(t), -1, np.int64), np.full(len(t), np.inf)
    j = np.clip(np.searchsorted(ref, t), 1, max(len(ref) - 1, 1))
    if len(ref) == 1:
        j = np.zeros(len(t), np.int64)
        return j, t - ref[0]
    near = np.where(np.abs(t - ref[j - 1]) <= np.abs(t - ref[j]), j - 1, j)
    return near.astype(np.int64), t - ref[near]


def count_in_window(ref: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Anzahl und erster Index der Elemente von ``ref`` (sortiert) in [lo, hi)."""
    a = np.searchsorted(ref, lo, "left")
    b = np.searchsorted(ref, hi, "left")
    return b - a, a


def window_index(t: np.ndarray, starts: np.ndarray, ends: np.ndarray) -> np.ndarray:
    """Index des Fensters [starts_j, ends_j] (sortiert, ggf. überlappend: das letzte beginnende), das t enthält; −1 sonst."""
    j = np.searchsorted(starts, t, "right") - 1
    ok = (j >= 0)
    jj = np.clip(j, 0, max(len(starts) - 1, 0))
    if len(starts) == 0:
        return np.full(len(t), -1, np.int64)
    ok &= t <= ends[jj]
    return np.where(ok, j, -1).astype(np.int64)
