"""Uhrabbildung Digitizer-Zeit → Harp-Zeit je Lauf: robuste Gerade plus optionale stückweise lineare Korrektur.

t_H = a0 + b·(T_D − x0) + Σ_j g_j(T_D)

* Gerade aus den Markerpaaren (Vorbericht 2, ROBUSTER_FIT): gemeinsame 10 MHz → b = 1, freilaufend b = 1/(1+ε).
* g_j: stetige stückweise lineare Korrekturen (Hutfunktionen). Der Knotenabstand W wird per Kreuzvalidierung
  gewählt (gerade/ungerade Paare); die Korrektur wird nur übernommen, wenn sie den Vorhersagefehler deutlich
  senkt (Irrfahrt der Phase, nicht lineare Drift).
* Verfeinerung (Architektur A): Herald/Trigger-Paare liefern eine zweite, präzisere Korrektur.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import kernels as K


@dataclass
class Piece:
    coef: np.ndarray
    x_start: float
    W: float
    source: str = "markers"

    def __call__(self, T: np.ndarray) -> np.ndarray:
        return K.piecewise_eval(T, self.coef, self.x_start, self.W)

    def to_dict(self) -> dict:
        return dict(coef=self.coef.tolist(), x_start=self.x_start, W=self.W, source=self.source)


@dataclass
class ClockMap:
    a0: float
    b: float
    x0: float
    cov: np.ndarray = field(default_factory=lambda: np.zeros((2, 2)))
    sigma_r: float = 0.0
    n_used: int = 0
    pieces: list[Piece] = field(default_factory=list)
    offset: float = 0.0                     # konstante Zusatzkorrektur (Verfeinerung)

    @property
    def a(self) -> float:
        """Achsenabschnitt bei T_D = 0."""
        return self.a0 - self.b * self.x0

    def H(self, T: np.ndarray) -> np.ndarray:
        T = np.asarray(T, float)
        y = self.a0 + self.b * (T - self.x0) + self.offset
        for p in self.pieces:
            y = y + p(T)
        return y

    def D(self, tH: np.ndarray) -> np.ndarray:
        tH = np.asarray(tH, float)
        T = self.x0 + (tH - self.a0 - self.offset) / self.b
        if self.pieces:
            for _ in range(4):
                T = T - (self.H(T) - tH) / self.b
        return T

    def sigma_H(self, T: np.ndarray) -> np.ndarray:
        """Unsicherheit der Geraden an T (ohne stückweise Anteile)."""
        T = np.asarray(T, float)
        dx = T - self.x0
        return np.sqrt(self.cov[0, 0] + 2 * dx * self.cov[0, 1] + dx ** 2 * self.cov[1, 1])

    @property
    def piecewise(self) -> bool:
        return any(p.source == "markers" for p in self.pieces)

    def to_dict(self) -> dict:
        return dict(a=self.a, a0=self.a0, b=self.b, x0=self.x0, sigma_a0=float(np.sqrt(self.cov[0, 0])),
                    sigma_b=float(np.sqrt(self.cov[1, 1])), sigma_r=self.sigma_r, n_used=self.n_used,
                    offset=self.offset, pieces=[p.to_dict() for p in self.pieces])

    @classmethod
    def from_dict(cls, d: dict) -> "ClockMap":
        cov = np.diag([d["sigma_a0"] ** 2, d["sigma_b"] ** 2])
        m = cls(d["a0"], d["b"], d["x0"], cov, d["sigma_r"], d["n_used"], offset=d.get("offset", 0.0))
        m.pieces = [Piece(np.asarray(p["coef"]), p["x_start"], p["W"], p.get("source", "markers")) for p in d["pieces"]]
        return m


def _cv_error(x, r, W, x_start, n_knots, smooth) -> float:
    ev = np.arange(len(x)) % 2 == 0
    err = []
    for tr in (ev, ~ev):
        c = K.piecewise_fit(x[tr], r[tr], x_start, W, n_knots, smooth)
        e = r[~tr] - K.piecewise_eval(x[~tr], c, x_start, W)
        err.append(np.mean(e ** 2))
    return float(np.mean(err))


def fit_piecewise_auto(x: np.ndarray, r: np.ndarray, min_points_per_knot: int = 20, gain: float = 0.9,
                       smooth: float = 1e-6, source: str = "markers") -> tuple[Piece | None, dict]:
    """Knotenabstand per Kreuzvalidierung; None, wenn die Gerade (r ≈ const) genügt."""
    x = np.asarray(x, float); r = np.asarray(r, float)
    n = len(x)
    info: dict = dict(candidates=[], chosen_W=None)
    if n < 4 * min_points_per_knot:
        return None, info
    T = float(x.max() - x.min())
    base = float(np.mean((r - r.mean()) ** 2)) * (1 + 1.0 / n)
    best = (base, None)
    W = T / 2
    while n * W / T >= min_points_per_knot and W > 0:
        nk = int(np.ceil(T / W)) + 1
        e = _cv_error(x, r, W, float(x.min()), nk, smooth)
        info["candidates"].append((W, e))
        if e < best[0]:
            best = (e, W)
        W /= 2
    info["cv_linear"] = base
    if best[1] is None or best[0] > gain * base:
        return None, info
    W = best[1]
    nk = int(np.ceil(T / W)) + 1
    c = K.piecewise_fit(x, r, float(x.min()), W, nk, smooth)
    info["chosen_W"] = W
    info["cv_piecewise"] = best[0]
    return Piece(c, float(x.min()), W, source), info


def fit_clock(TD: np.ndarray, tH: np.ndarray, allow_piecewise: bool = True, clip: float = 5.0,
              floor: float = 0.0) -> tuple[ClockMap, dict]:
    """Uhrabbildung aus Markerpaaren (T_D, t_H)."""
    lin = K.robust_linear(TD, tH, clip=clip, floor=floor)
    m = ClockMap(lin["a0"], lin["b"], lin["x0"], lin["cov"], lin["sigma_r"], int(lin["used"].sum()))
    used = lin["used"]
    info = dict(model="linear", n_pairs=len(TD), n_outliers=int((~used).sum()), sigma_r_linear=lin["sigma_r"])
    res = lin["residuals"]
    if allow_piecewise:
        piece, pinfo = fit_piecewise_auto(TD[used], res[used])
        info["piecewise"] = pinfo
        if piece is not None:
            m.pieces.append(piece)
            res = tH - m.H(TD)
            # Ausreißer gegen das stückweise Modell neu bestimmen
            c, s, ok = K.robust_center(res, k=clip, floor=floor)
            info.update(model="piecewise", W=piece.W)
            m.sigma_r = float(np.std(res[ok], ddof=1)) if ok.sum() > 2 else s
            info["n_outliers"] = int((~ok).sum())
            used = ok
    info["sigma_r"] = m.sigma_r
    info["residuals"] = tH - m.H(TD)
    info["used"] = used
    return m, info
