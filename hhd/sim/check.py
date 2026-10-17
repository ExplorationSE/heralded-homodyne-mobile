"""Referenzauswertung für die Validierung des Simulators (nicht die produktive Auswertung).

Nutzt die **Wahrheitsdatei** für die Modenposition (ideale Synchronisation) und prüft damit, ob die
erzeugten Wellenformen den Sollzustand tragen: Vakuumnormierung aus den K-V-Blöcken, Projektion auf die
Modenfunktion, Photonenzahlverteilung per EM (phasengemittelt), W(0,0) = Σ(−1)ⁿρₙₙ/π, Bootstrap-Fehler,
bei bekannter Phase θ ein Fit ⟨x⟩(θ) = A·cos(θ − φ).

Vakuumnormierung (Gegenmaßnahmen gegen Drift zwischen getrennten Läufen, Gesamtplan v1.4):

* ``pooled``       – gemeinsame Kalibrierung aller K-V-Läufe der Sitzung (Standard);
* ``interpolated`` – Vakuumvarianz zeitlich linear zwischen den K-V-Läufen interpoliert;
* ``monitor``      – gemeinsame Kalibrierung, je Lauf mit dem LO-Monitor (ADQ32-Kanal B, sonst externer
  Leistungsmesser) skaliert (σ ∝ √P_LO;
  der elektronische Rauschanteil 1/S wird dabei vernachlässigt).

Der Vergleich ``comparison`` fasst die Läufe mit Herald-Trigger (A) und ohne Herald (B) zusammen und
weist Driftindikatoren aus: A-B-A-Konsistenz der A-Läufe, Änderung der Vakuumskala zwischen erstem und
letztem K-V-Lauf, Änderung der LO-Leistung laut Monitor.

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


NORM_MODES = ("pooled", "interpolated", "monitor")


class SessionReader:
    def __init__(self, run_dir: str | Path):
        self.dir = Path(run_dir)
        self.manifest = json.loads((self.dir / "session.json").read_text(encoding="utf-8"))
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
        return [b["id"] for b in self.manifest["runs"]]

    def attrs(self, bid):
        return self.dig[f"blocks/{bid}"].attrs

    def t_mid(self, bid) -> float:
        a = self.attrs(bid)
        return float(a["t_start_s"]) + 0.5 * float(a["duration_s"])

    def lo_dark_b(self) -> float:
        """Dunkelwert von Kanal B: aus Läufen mit LO aus (electronic), sonst der nominelle Wert der Gerätedatei."""
        if not hasattr(self, "_dark_b"):
            dark = [self.dig[f"blocks/{b}/records/data_b"][:].mean() for b in self.blocks()
                    if self.attrs(b)["dataset_type"] == "electronic" and "data_b" in self.dig[f"blocks/{b}/records"]
                    and self.dig[f"blocks/{b}/records/data_b"].shape[0]]
            self._dark_b = float(np.mean(dark)) if dark else float(self.dig["device"].attrs.get("lo_monitor_dark_lsb", 0.0))
        return self._dark_b

    def lo_monitor(self, bid) -> float:
        """mittlere LO-Leistung im Lauf (relativ): Kanal B (Photodiode) bevorzugt, sonst externer Leistungsmesser."""
        g = self.dig[f"blocks/{bid}"]
        if "data_b" in g["records"] and g["records/data_b"].shape[0]:
            nom = float(self.dig["device"].attrs.get("lo_monitor_lsb_nominal", 1.0))
            return float((g["records/data_b"][:].mean() - self.lo_dark_b()) / nom)
        return float(g["monitor/lo_power_rel"][:].mean()) if "monitor" in g else float("nan")

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

    def vacuum_runs(self):
        return [b for b in self.blocks() if self.attrs(b)["dataset_type"] == "vacuum"]

    def vacuum_norm(self):
        """gemeinsame Kalibrierung: (Offset, Skala, n) aus allen K-V-Läufen der Sitzung."""
        vac = self.vacuum_runs()
        if not vac:
            raise ValueError("kein Vakuumlauf (K-V) vorhanden")
        datas = [self.records(b)[0] for b in vac]
        off = float(np.mean([d.mean() for d in datas]))
        xs = np.concatenate([self.project(b, off=off) for b in vac])
        return off, float(np.sqrt(xs.var() / 0.5)), len(xs)

    def vacuum_per_run(self, off: float) -> list[dict]:
        """Vakuumskala je K-V-Lauf (für Interpolation und Driftindikator)."""
        out = []
        for b in self.vacuum_runs():
            x = self.project(b, off=off)
            out.append(dict(id=b, t=self.t_mid(b), var=float(x.var()), n=len(x), lo_monitor=self.lo_monitor(b)))
        return out

    def scale_for(self, bid, mode: str, norm, per_run=None) -> float:
        off, s, _ = norm
        if mode == "pooled":
            return s
        if mode == "interpolated":
            per_run = per_run if per_run is not None else self.vacuum_per_run(off)
            t = np.array([v["t"] for v in per_run]); var = np.array([v["var"] for v in per_run])
            return float(np.sqrt(np.interp(self.t_mid(bid), t, var) / 0.5))
        if mode == "monitor":
            per_run = per_run if per_run is not None else self.vacuum_per_run(off)
            lo_vac = np.average([v["lo_monitor"] for v in per_run], weights=[v["n"] for v in per_run])
            lo = self.lo_monitor(bid)
            if not (np.isfinite(lo) and lo > 0 and np.isfinite(lo_vac)):
                raise ValueError(f"{bid}: kein LO-Monitor für die Normierung")
            return float(s * np.sqrt(lo / lo_vac))
        raise ValueError(f"Normierung {mode!r} unbekannt ({', '.join(NORM_MODES)})")

    def quadratures(self, bid, norm=None, mode: str = "pooled", per_run=None):
        norm = norm or self.vacuum_norm()
        return self.project(bid, off=norm[0], s=self.scale_for(bid, mode, norm, per_run))

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


RunReader = SessionReader      # frühere Bezeichnung


def _diff(a: dict, b: dict, key: str) -> dict:
    """Differenz a − b einer Kenngröße (W00 oder var_x) mit Fehler und z-Wert."""
    va, vb = a[key], b[key]
    sa = a["W00_err"] if key == "W00" else a["var_err"]
    sb = b["W00_err"] if key == "W00" else b["var_err"]
    d = va - vb
    s = float(np.hypot(sa, sb))
    return dict(diff=float(d), err=s, z=float(d / s) if s > 0 else float("nan"))


def check_run(run_dir: str | Path, n_boot: int = 100, norm_mode: str = "pooled") -> dict:
    """Bewertet alle Läufe einer Sitzung gegen ihre Sollwerte und vergleicht A (Herald) mit B (ohne Herald).

    Je Block wird die Hauptgruppe bewertet (Herald-, Zustands-, Zufalls- bzw. periodisch getriggerte Records);
    in heralded Blöcken eingestreute Zufallstrigger erscheinen getrennt unter ``<block>/random``.
    W(0,0) aus der phasengemittelten EM-Rekonstruktion wird nur bei gleichverteilter Phase bewertet
    (phase.mode random oder ramp, bzw. phasenunempfindlicher Zustand).
    """
    if norm_mode not in NORM_MODES:
        raise ValueError(f"norm_mode: {', '.join(NORM_MODES)}")
    rr = SessionReader(run_dir)
    try:
        norm = rr.vacuum_norm()
        per_run = rr.vacuum_per_run(norm[0])
        out = dict(session_id=rr.manifest.get("session_id"), campaign_id=rr.manifest.get("campaign_id"),
                   norm_mode=norm_mode, vacuum_norm=dict(offset=norm[0], scale=norm[1], n=norm[2]), blocks={},
                   scales={})
        xs_role: dict[str, list] = {"heralded": [], "unheralded": []}
        exp_role: dict[str, list] = {"heralded": [], "unheralded": []}
        for bid in rr.blocks():
            exp = rr.expected(bid)
            attrs = rr.attrs(bid)
            kind = rr.tru[f"blocks/{bid}/records/kind"][:]
            if attrs["kind"] == "electronic" and norm_mode != "pooled":
                scale = norm[1]
            else:
                scale = rr.scale_for(bid, norm_mode, norm, per_run)
            out["scales"][bid] = scale
            x_all = rr.project(bid, off=norm[0], s=scale)
            th_all = rr.theta(bid)
            main = {"heralded": TRIG_HERALD, "herald_blocked": TRIG_HERALD, "random": TRIG_RANDOM,
                    "state": TRIG_STATE}.get(attrs["kind"], TRIG_PERIODIC)
            role = attrs["comparison_role"]
            if role in xs_role:
                xs_role[role].append(x_all[kind == main]); exp_role[role].append((int((kind == main).sum()), exp))
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
                r["run_id"] = attrs["run_id"] if name == bid else ""
                out["blocks"][name] = r
        out["comparison"] = _comparison(rr, out, xs_role, exp_role, per_run, n_boot)
        return out
    finally:
        rr.close()


def interpolation_weights(t: float, t_kv: np.ndarray) -> np.ndarray:
    """Gewichte der K-V-Läufe bei linearer Interpolation (konstant außerhalb), wie ``np.interp``."""
    w = np.zeros(len(t_kv))
    j = int(np.searchsorted(t_kv, t))
    if j == 0:
        w[0] = 1.0
    elif j >= len(t_kv):
        w[-1] = 1.0
    else:
        f = (t - t_kv[j - 1]) / (t_kv[j] - t_kv[j - 1])
        w[j - 1], w[j] = 1 - f, f
    return w


def aba_calibration_variance(rr, per_run: list, first: str, last: str, v_a: float, v_b: float) -> float:
    """Varianz von Var_b − Var_a durch die Statistik der K-V-Läufe bei interpolierter Normierung.

    v = m₂/s² mit s² = Σ_k w_k·V_k: δv/v = −Σ_k w_k·ε_k, ε_k relative Fehler von V_k (Varianz 2/n_k)."""
    t_kv = np.array([v["t"] for v in per_run]); n_kv = np.array([v["n"] for v in per_run], float)
    wa = interpolation_weights(rr.t_mid(first), t_kv); wb = interpolation_weights(rr.t_mid(last), t_kv)
    return float(np.sum((v_b * wb - v_a * wa) ** 2 * 2 / n_kv))


def _comparison(rr: SessionReader, out: dict, xs_role: dict, exp_role: dict, per_run: list, n_boot: int) -> dict:
    """Vergleich getrennter Läufe (ED-12) und Driftindikatoren."""
    comp: dict = {}
    for role in ("heralded", "unheralded"):
        if not xs_role[role]:
            continue
        x = np.concatenate(xs_role[role])
        r = summarize(x, n_boot=n_boot)
        n_tot = sum(n for n, _ in exp_role[role])
        e = {k: float(sum(n * ex[k] for n, ex in exp_role[role]) / n_tot)
             for k in ("W00", "var_x") if all(k in ex for _, ex in exp_role[role])}
        r["expected"] = e
        if "W00" in e and r["W00_err"] > 0:
            r["W00_pull"] = (r["W00"] - e["W00"]) / r["W00_err"]
        r["runs"] = [b for b in rr.blocks() if rr.attrs(b)["comparison_role"] == role]
        comp[role] = r
    if "heralded" in comp and "unheralded" in comp:
        comp["heralded_minus_unheralded"] = dict(W00=_diff(comp["heralded"], comp["unheralded"], "W00"),
                                                 var_x=_diff(comp["heralded"], comp["unheralded"], "var_x"))
    # A-B-A: erster gegen letzten Lauf mit Herald-Trigger
    hr = [b for b in rr.blocks() if rr.attrs(b)["comparison_role"] == "heralded" and b in out["blocks"]
          and "W00" in out["blocks"][b]]
    drift: dict = {}
    if len(hr) >= 2:
        a, b = out["blocks"][hr[0]], out["blocks"][hr[-1]]
        drift["aba"] = dict(first=hr[0], last=hr[-1], W00=_diff(b, a, "W00"), var_x=_diff(b, a, "var_x"))
        if out.get("norm_mode") == "interpolated" and len(per_run) >= 2:
            # Skalen aus verschiedenen K-V-Läufen: deren Statistik geht in die Differenz ein (sonst Fehlalarme)
            cal = aba_calibration_variance(rr, per_run, hr[0], hr[-1], a["var_x"], b["var_x"])
            for key, fac in (("var_x", 1.0), ("W00", 2 / np.pi)):          # δW(0,0) ≈ −(2/π)·δVar (|1⟩-nah)
                dd = drift["aba"][key]
                dd["err_stat"] = dd["err"]
                dd["err"] = float(np.hypot(dd["err"], fac * np.sqrt(cal)))
                dd["z"] = float(dd["diff"] / dd["err"]) if dd["err"] > 0 else float("nan")
            drift["aba"]["calibration_included"] = True
    if len(per_run) >= 2:
        v0, v1 = per_run[0], per_run[-1]
        rel = v1["var"] / v0["var"] - 1
        err = float(np.sqrt(2 / v0["n"] + 2 / v1["n"]))
        drift["vacuum"] = dict(first=v0["id"], last=v1["id"], var_rel_change=float(rel), err=err, z=float(rel / err))
    lo = [(b, rr.lo_monitor(b)) for b in rr.blocks() if np.isfinite(rr.lo_monitor(b)) and rr.lo_monitor(b) > 0]
    if len(lo) >= 2:
        drift["lo_monitor"] = dict(first=lo[0][0], last=lo[-1][0], rel_change=float(lo[-1][1] / lo[0][1] - 1),
                                   per_run={b: v for b, v in lo})
    flags = []
    if "aba" in drift and max(abs(drift["aba"]["W00"]["z"]), abs(drift["aba"]["var_x"]["z"])) > 3:
        flags.append("A-B-A: erster und letzter Herald-Lauf weichen > 3σ ab (Drift zwischen Läufen?)")
    if "vacuum" in drift and abs(drift["vacuum"]["z"]) > 3:
        flags.append("K-V: Vakuumvarianz ändert sich > 3σ (LO-Leistung/Verstärkung driftet)")
    if "lo_monitor" in drift and abs(drift["lo_monitor"]["rel_change"]) > 0.01:
        flags.append("LO-Monitor: Leistung ändert sich um mehr als 1 %")
    drift["flags"] = flags
    comp["drift"] = drift
    return comp
