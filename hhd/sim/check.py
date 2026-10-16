"""Referenzauswertung für die Validierung des Simulators (nicht die produktive Auswertung).

Nutzt die **Wahrheitsdatei** für die Modenposition (ideale Synchronisation) und prüft damit, ob die
erzeugten Wellenformen den Sollzustand tragen: Vakuumnormierung aus den K-V-Blöcken, Projektion auf die
Modenfunktion, Photonenzahlverteilung per EM (phasengemittelt), W(0,0) = Σ(−1)ⁿρₙₙ/π, Bootstrap-Fehler,
bei bekannter Phase θ ein Fit ⟨x⟩(θ) = A·cos(θ − φ).

Synchronisation aus den Markern (ohne Wahrheit), Kalibrierung und Rekonstruktion für Messdaten folgen in
AP3–AP7 (``hhd.sync``, ``hhd.calib``, ``hhd.modes``, ``hhd.recon``).
"""
from __future__ import annotations

import json
from pathlib import Path

import h5py
import numpy as np

from .config import SimConfig
from .digitizer import TRIG_HERALD, TRIG_PERIODIC, TRIG_RANDOM, TRIG_STATE, mode_vector
from .states import hermite_function


def em_photon_number(x: np.ndarray, nmax: int = 10, iters: int = 400) -> np.ndarray:
    P = np.stack([hermite_function(n, x) ** 2 for n in range(nmax + 1)])
    rho = np.ones(nmax + 1) / (nmax + 1)
    for _ in range(iters):
        pr = rho @ P
        rho = rho * (P / pr).mean(1)
        rho /= rho.sum()
    return rho


def wigner00(p: np.ndarray) -> float:
    return float(np.sum(p * (-1.0) ** np.arange(len(p))) / np.pi)


class RunReader:
    def __init__(self, run_dir: str | Path):
        self.dir = Path(run_dir)
        self.manifest = json.loads((self.dir / "run.json").read_text(encoding="utf-8"))
        self.cfg = SimConfig.from_dict(self.manifest["config"])
        self.dig = h5py.File(self.dir / "digitizer.h5", "r")
        self.tru = h5py.File(self.dir / "truth.h5", "r")
        ck = self.tru["clock"]
        self.eps, self.t_arm = float(ck.attrs["eps"]), float(ck.attrs["t_arm_s"])
        self.tg, self.phig = ck["t_grid"][:], ck["phi"][:]
        dev = self.dig["device"].attrs
        self.fs, self.tu, self.L = float(dev["sampling_rate_Hz"]), float(dev["time_unit_s"]), int(dev["record_length"])

    def close(self):
        self.dig.close(); self.tru.close()

    def clk(self, t):
        return (1 + self.eps) * (np.asarray(t) - self.t_arm) + np.interp(t, self.tg, self.phig)

    def blocks(self):
        return [b["id"] for b in self.manifest["blocks"]]

    def expected(self, bid):
        return json.loads(self.tru[f"blocks/{bid}"].attrs["expected_json"])

    def records(self, bid):
        r = self.dig[f"blocks/{bid}/records"]
        return r["data"][:], r["timestamp"][:], r["record_start"][:]

    def eval_positions(self, bid) -> np.ndarray:
        """Digitizer-Zeit (s) der zu bewertenden Mode je Record (aus der Wahrheit)."""
        c = self.cfg
        g = self.tru[f"blocks/{bid}"]
        kind = g["records/kind"][:]
        idx = g["records/source_index"][:]
        pos = np.full(len(kind), np.nan)
        dS, dT, dRT = c.digitizer.d_S, c.digitizer.d_T, c.digitizer.d_RT
        th = g["heralds/t_true"][:]
        m = kind == TRIG_HERALD
        pos[m] = self.clk(th[idx[m]] + dS)
        m = kind == TRIG_RANDOM            # gleiche relative Lage wie eine Herald-Mode zu ihrem Trigger
        pos[m] = self.clk(g["random/t_true"][:][idx[m]] + dRT - dT + dS)
        m = kind == TRIG_STATE
        pos[m] = self.clk(g["state/t_true"][:][idx[m]] + dS)
        m = kind == TRIG_PERIODIC          # Vakuum/elektronisch: Mitte des Records
        if m.any():
            _, ts, rs = self.records(bid)
            pos[m] = (ts[m] + rs[m]) * self.tu + 0.5 * self.L / self.fs
        return pos

    def project(self, bid, pos=None, off=0.0, s=1.0) -> np.ndarray:
        data, ts, rs = self.records(bid)
        pos = self.eval_positions(bid) if pos is None else pos
        k = np.arange(self.L)
        x = np.empty(len(data))
        c = self.cfg.source
        for j in range(len(data)):
            tk = (ts[j] + rs[j]) * self.tu + k / self.fs
            g = mode_vector(c.mode_shape, c.gamma, self.fs, tk, pos[j])
            g = g / np.linalg.norm(g)
            x[j] = g @ (data[j].astype(float) - off)
        return x / s

    def vacuum_norm(self):
        vac = [b for b in self.blocks() if self.dig[f"blocks/{b}"].attrs["dataset_type"] == "vacuum"]
        if not vac:
            raise ValueError("kein Vakuumblock (K-V) vorhanden")
        datas = [self.records(b)[0] for b in vac]
        off = float(np.mean([d.mean() for d in datas]))
        xs = np.concatenate([self.project(b, off=off) for b in vac])
        return off, float(np.sqrt(xs.var() / 0.5)), len(xs)

    def quadratures(self, bid, norm=None):
        off, s, _ = norm or self.vacuum_norm()
        return self.project(bid, off=off, s=s)

    def theta(self, bid):
        """wahre Phase der bewerteten Mode je Record (nur Testzustände)."""
        g = self.tru[f"blocks/{bid}"]
        kind = g["records/kind"][:]
        idx = g["records/source_index"][:]
        mk, ms = g["modes/kind"][:], g["modes/source_index"][:]
        th_mode = g["modes/theta"][:]
        th = np.full(len(kind), np.nan)
        st = np.flatnonzero(mk == 2)
        lut = dict(zip(ms[st], th_mode[st]))
        for j in np.flatnonzero(kind == TRIG_STATE):
            th[j] = lut.get(idx[j], np.nan)
        return th


def summarize(x: np.ndarray, n_boot: int = 100, rng=None, nmax: int = 10) -> dict:
    rng = rng or np.random.default_rng(7)
    p = em_photon_number(x, nmax=nmax)
    w = wigner00(p)
    bs = []
    for _ in range(n_boot):
        xb = x[rng.integers(0, len(x), len(x))]
        bs.append(wigner00(em_photon_number(xb, nmax=nmax, iters=150)))
    var = float(x.var())
    return dict(n=int(len(x)), mean_x=float(x.mean()), var_x=var, var_err=float(var * np.sqrt(2 / max(len(x) - 1, 1))),
                W00=w, W00_err=float(np.std(bs)), p_n=p[:4].tolist())


def phase_fit(x: np.ndarray, theta: np.ndarray) -> dict:
    """⟨x⟩(θ) = a·cos θ + b·sin θ = A·cos(θ − φ); kleinste Quadrate mit Standardfehlern."""
    m = np.isfinite(theta)
    X = np.stack([np.cos(theta[m]), np.sin(theta[m])], 1)
    coef, *_ = np.linalg.lstsq(X, x[m], rcond=None)
    res = x[m] - X @ coef
    cov = np.linalg.inv(X.T @ X) * res.var()
    a, b = coef
    A = float(np.hypot(a, b))
    return dict(A=A, phi=float(np.arctan2(b, a)), A_err=float(np.sqrt((a * a * cov[0, 0] + b * b * cov[1, 1]) / A ** 2)),
                residual_var=float(res.var()), n=int(m.sum()))


def check_run(run_dir: str | Path, n_boot: int = 100) -> dict:
    """Bewertet alle Blöcke eines Laufs gegen ihre Sollwerte.

    Je Block wird die Hauptgruppe bewertet (Herald-, Zustands-, Zufalls- bzw. periodisch getriggerte Records);
    in heralded Blöcken eingestreute Zufallstrigger erscheinen getrennt unter ``<block>/random``.
    W(0,0) aus der phasengemittelten EM-Rekonstruktion wird nur bei gleichverteilter Phase bewertet
    (phase.mode random oder ramp, bzw. phasenunempfindlicher Zustand).
    """
    rr = RunReader(run_dir)
    try:
        norm = rr.vacuum_norm()
        out = dict(vacuum_norm=dict(offset=norm[0], scale=norm[1], n=norm[2]), blocks={})
        for bid in rr.blocks():
            exp = rr.expected(bid)
            attrs = rr.dig[f"blocks/{bid}"].attrs
            kind = rr.tru[f"blocks/{bid}/records/kind"][:]
            x_all = rr.quadratures(bid, norm)
            th_all = rr.theta(bid)
            main = {"heralded": TRIG_HERALD, "herald_blocked": TRIG_HERALD, "random": TRIG_RANDOM,
                    "state": TRIG_STATE}.get(attrs["kind"], TRIG_PERIODIC)
            groups = [(bid, kind == main, exp)]
            if attrs["kind"] == "heralded" and np.any(kind == TRIG_RANDOM):
                groups.append((f"{bid}/random", kind == TRIG_RANDOM, dict(W00=1 / np.pi, var_x=0.5)))
            uniform = attrs["phase_mode"] in ("random", "ramp") or not exp.get("phase_sensitive", False)
            for name, m, e in groups:
                x, th = x_all[m], th_all[m]
                if len(x) < 20:
                    out["blocks"][name] = dict(n=int(len(x)), note="zu wenige Records")
                    continue
                r = summarize(x, n_boot=n_boot)
                r["expected"] = {k: e[k] for k in ("W00", "var_x", "eta_eff", "var_ratio_to_vacuum") if k in e}
                if not uniform:
                    r["expected"].pop("W00", None)
                    r["note"] = "Phase nicht gleichverteilt: W(0,0) aus phasengemittelter EM nicht bewertet"
                if "W00" in r["expected"] and r["W00_err"] > 0:
                    r["W00_pull"] = (r["W00"] - e["W00"]) / r["W00_err"]
                if np.isfinite(th).sum() > 20 and e.get("phase_sensitive"):
                    a = complex(e["state"].get("alpha_re", 0), e["state"].get("alpha_im", 0))
                    r["phase_fit"] = phase_fit(x, th)
                    r["expected"]["A"] = float(np.sqrt(2 * e["eta_total"]) * abs(a)) if e["state"]["type"] == "coherent" else 0.0
                    r["expected"]["phi"] = float(np.angle(a))
                out["blocks"][name] = r
        return out
    finally:
        rr.close()
