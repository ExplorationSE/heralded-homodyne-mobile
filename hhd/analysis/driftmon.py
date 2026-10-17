"""Drift-Überwachung im Datenstrom (Echtzeit, konstanter Speicher): LO-Leistung, Vakuumvarianz und η.

Gefüttert wird je Datenblock (Scheibe des Datenstrom-Simulators oder Chunk der Live-Geräte) mit den Records von
Kanal A und B; gehalten werden nur laufende Summen (keine Records, keine Quadraturlisten).

* **LO** (Kanal B, LO-Monitor): Mittel je Fenster (``window_s``) minus Dunkelwert, relativ zum ersten K-V-Lauf;
  Warnung bei |ΔP/P| > ``lo_warn_rel`` (Standard 1 %).
* **Vakuum**: Varianz der Quadratur (nominelle Mode am Ort Trigger + Δ_T) je K-V-Lauf, z-Test gegen den ersten
  K-V (Varianz ∝ P_LO, also auch ein zweiter, unabhängiger LO-Indikator).
* **η**: Überschussvarianz der geheraldeten Läufe ``e = Var/Var_vac − ½`` (Var_vac aus dem letzten K-V, mit dem
  LO-Monitor auf die LO-Leistung des Laufs umgerechnet); für das verlustbehaftete Fock-1 gilt e = η_eff, also
  ``e/e₀`` = relative η-Änderung gegen den ersten A-Lauf, z-Test mit dem Fehler aus beiden Läufen.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..modes.extract import ModeSpec, project_records, template_bank


@dataclass
class _Acc:
    n: int = 0
    s1: float = 0.0
    s2: float = 0.0

    def add(self, x: np.ndarray) -> None:
        self.n += len(x); self.s1 += float(x.sum()); self.s2 += float((x * x).sum())

    @property
    def var(self) -> float:
        if self.n < 2:
            return float("nan")
        m = self.s1 / self.n
        return max(self.s2 / self.n - m * m, 0.0) * self.n / (self.n - 1)


@dataclass
class RunDrift:
    block_id: str
    kind: str
    t_start_s: float
    q: _Acc = field(default_factory=_Acc)
    b_sum: float = 0.0
    b_n: int = 0
    lo_rel: float = float("nan")
    var_vac_ref: float = float("nan")
    excess: float = float("nan")
    excess_err: float = float("nan")
    eta_rel: float = float("nan")
    eta_z: float = float("nan")
    vac_z: float = float("nan")

    def to_dict(self) -> dict:
        f = lambda x: None if not np.isfinite(x) else round(float(x), 6)
        return dict(block_id=self.block_id, kind=self.kind, t_start_s=f(self.t_start_s), n=self.q.n,
                    var=f(self.q.var), lo_rel=f(self.lo_rel), excess=f(self.excess), excess_err=f(self.excess_err),
                    eta_rel=f(self.eta_rel), eta_z=f(self.eta_z), vac_z=f(self.vac_z))


class DriftMonitor:
    def __init__(self, fs: float = 2.5e9, delta_T_s: float = 77.3e-9, gamma: float = 1 / 40e-9,
                 dark_b: float | None = None, window_s: float = 5.0, lo_warn_rel: float = 0.01, z_warn: float = 4.0):
        self.fs, self.dT = fs, delta_T_s
        self.bank = template_bank(ModeSpec("double_exponential", gamma, fs))
        self.dark_b = dark_b
        self.window_s, self.lo_warn_rel, self.z_warn = window_s, lo_warn_rel, z_warn
        self.offset: float | None = None
        self.runs: list[RunDrift] = []
        self.cur: RunDrift | None = None
        self.lo_ref: float | None = None
        self.vac: list[tuple[float, int, float]] = []          # (Varianz, n, LO relativ) je K-V
        self.eta_ref: tuple[float, float] | None = None
        self.lo_trace: list[tuple[float, float]] = []          # (t, LO relativ) je Fenster, begrenzt
        self.warnings: list[str] = []
        self._win = None

    # ------------------------------------------------------------------ Lauf
    def begin_run(self, block_id: str, kind: str, t_start_s: float) -> None:
        self.cur = RunDrift(block_id, kind, t_start_s)
        self.runs.append(self.cur)
        self._win = [t_start_s, 0.0, 0]

    def feed(self, data: np.ndarray, trig_D: np.ndarray, first_D: np.ndarray, data_b: np.ndarray | None,
             t_s: float) -> None:
        r = self.cur
        if r is None or len(trig_D) == 0:
            return
        data = np.asarray(data)
        if self.offset is None and r.kind in ("vacuum", "electronic"):
            self.offset = float(data.mean())
        if r.kind != "electronic":
            pos = (np.asarray(trig_D) + self.dT - np.asarray(first_D)) * self.fs
            q, cov = project_records(data, pos, self.bank, self.offset or 0.0)
            r.q.add(q[cov >= 0.99])
        if data_b is not None and len(data_b):
            mb = np.asarray(data_b, float).mean(axis=1)
            r.b_sum += float(mb.sum()); r.b_n += len(mb)
            w = self._win
            w[1] += float(mb.sum()); w[2] += len(mb)
            if t_s - w[0] >= self.window_s and w[2]:
                lo = self._lo_rel(w[1] / w[2])
                if np.isfinite(lo):
                    self._trace(t_s, lo)
                    if abs(lo - 1) > self.lo_warn_rel:
                        self._warn(f"LO-Leistung {100 * (lo - 1):+.2f} % gegen ersten K-V (t = {t_s:.0f} s, {r.block_id})")
                self._win = [t_s, 0.0, 0]

    def _lo_rel(self, b: float) -> float:
        if self.lo_ref is None:
            return float("nan")
        dk = self.dark_b or 0.0
        return (b - dk) / (self.lo_ref - dk)

    def _trace(self, t, lo) -> None:
        self.lo_trace.append((round(float(t), 3), round(float(lo), 6)))
        if len(self.lo_trace) > 2000:
            self.lo_trace = self.lo_trace[::2]

    def _warn(self, msg: str) -> None:
        if not self.warnings or self.warnings[-1] != msg:
            self.warnings.append(msg)
            self.warnings = self.warnings[-200:]

    def end_run(self) -> RunDrift | None:
        r, self.cur = self.cur, None
        if r is None:
            return None
        b = r.b_sum / r.b_n if r.b_n else float("nan")
        if r.kind == "electronic" and r.b_n:
            self.dark_b = b
        if r.kind == "vacuum" and self.lo_ref is None and r.b_n:
            self.lo_ref = b
        r.lo_rel = self._lo_rel(b) if r.b_n else float("nan")
        if r.kind == "vacuum" and r.q.n > 1:
            lo = r.lo_rel if np.isfinite(r.lo_rel) else 1.0
            self.vac.append((r.q.var, r.q.n, lo))
            v0, n0, _ = self.vac[0]
            r.vac_z = (r.q.var - v0) / (v0 * np.sqrt(2 / n0 + 2 / r.q.n))
            if len(self.vac) > 1 and abs(r.vac_z) > self.z_warn:
                self._warn(f"Vakuumvarianz {r.block_id}: {r.vac_z:+.1f}σ gegen ersten K-V "
                           f"({100 * (r.q.var / v0 - 1):+.2f} %)")
        if r.kind == "heralded" and r.q.n > 1 and self.vac:
            v, nv, lo_v = self.vac[-1]
            lo_a = r.lo_rel if np.isfinite(r.lo_rel) else lo_v
            vref = v * lo_a / lo_v                                 # Vakuumvarianz ∝ P_LO
            r.var_vac_ref = vref
            x = r.q.var / vref
            r.excess = 0.5 * x - 0.5                               # Var/Var_vac = (½ + e)/½
            r.excess_err = 0.5 * x * np.sqrt(2 / r.q.n + 2 / nv)
            if self.eta_ref is None:
                self.eta_ref = (r.excess, r.excess_err)
                r.eta_rel, r.eta_z = 1.0, 0.0
            else:
                e0, s0 = self.eta_ref
                r.eta_rel = r.excess / e0
                r.eta_z = (r.excess - e0) / np.hypot(s0, r.excess_err)
                if abs(r.eta_z) > self.z_warn:
                    self._warn(f"η-Drift {r.block_id}: {100 * (r.eta_rel - 1):+.1f} % gegen ersten A-Lauf "
                               f"({r.eta_z:+.1f}σ)")
        return r

    def trend(self) -> dict:
        """Gewichtete Geraden über die Läufe: relative η-Änderung (A-Läufe) und LO (alle Läufe mit LO) je Sekunde,
        mit z-Wert der Steigung und der Änderung über die überwachte Zeit."""
        out = {}
        A = [r for r in self.runs if r.kind == "heralded" and np.isfinite(r.excess) and r.excess_err > 0]
        if len(A) >= 2 and self.eta_ref:
            e0 = self.eta_ref[0]
            t = np.array([r.t_start_s for r in A]); y = np.array([r.excess for r in A]) / e0
            w = 1 / (np.array([r.excess_err for r in A]) / e0) ** 2
            out["eta"] = _wfit(t, y, w)
        L = [r for r in self.runs if r.kind != "electronic" and np.isfinite(r.lo_rel)]
        if len(L) >= 2:
            t = np.array([r.t_start_s for r in L]); y = np.array([r.lo_rel for r in L])
            out["lo"] = _wfit(t, y, np.ones_like(t), known_errors=False) | dict(last=float(y[-1]))
        return out

    def summary(self) -> dict:
        return dict(runs=[r.to_dict() for r in self.runs], lo_trace=self.lo_trace, warnings=self.warnings,
                    dark_b=self.dark_b, lo_ref_b=self.lo_ref, trend=self.trend())


def _wfit(t: np.ndarray, y: np.ndarray, w: np.ndarray, known_errors: bool = True) -> dict:
    """Gewichtete Gerade. ``known_errors``: Gewichte = 1/σ² (Fehler mindestens aus σ, bei χ²/dof > 1 skaliert);
    sonst Fehler aus der Streuung der Residuen."""
    W = w.sum(); tm = (w * t).sum() / W; ym = (w * y).sum() / W
    Stt = (w * (t - tm) ** 2).sum()
    b = (w * (t - tm) * (y - ym)).sum() / Stt
    a = ym - b * tm
    res = y - (a + b * t)
    dof = max(len(t) - 2, 1)
    chi2 = float((w * res ** 2).sum() / dof)
    if known_errors:
        sb = np.sqrt(1 / Stt * max(chi2, 1.0)) if len(t) > 2 else np.sqrt(1 / Stt)
    else:
        sb = np.sqrt(chi2 / Stt) if len(t) > 2 else float("inf")
    span = float(t.max() - t.min())
    return dict(slope_per_s=float(b), slope_err=float(sb), z=float(b / sb) if sb > 0 else float("nan"),
                change_over_span=float(b * span), span_s=span, intercept=float(a))
