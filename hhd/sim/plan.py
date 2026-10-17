"""Laufplan einer Sitzung (ED-12): Aufbau, Statistik, Datenbudget, Driftfehler und Anpassungsregel (Vorschlag M1).

Ein Laufplan ist eine Folge von Läufen (``blocks``)::

    K-V₀ → K-H → [A₁ → B₁ (→ K-Vᵢ alle ``kv_every`` Zyklen)] … → A_{n+1} → K-V_end (→ K-H_end)

mit ``n_cycles`` A-B-Zyklen und einem abschließenden A-Lauf (A-B-A-Verschachtelung). Die Funktionen hier
liefern

* :func:`make_plan` – die Blockliste für ``SimConfig.blocks``;
* :func:`statistics` – erwartete Segmentzahlen, Messzeiten, Datenmengen und statistische Fehler
  (η, Var x, W(0,0), Vakuumskala, K-H-Empfindlichkeit);
* :func:`drift_error` – erwarteter Fehler der Normierung durch Drift (exakt für Irrfahrt, lineare und
  periodische Drift; Normierung ``pooled``, ``interpolated``, ``monitor`` wie in :mod:`hhd.sim.check`);
* :func:`adapt_plan` – Anpassungsregel aus einer Allan-Analyse der Stabilitätsmessung (AP14).

Statistische Grundlagen (Vakuumvarianz ½, phasengemittelter Zustand ρ = (1−η)|0⟩⟨0| + η|1⟩⟨1|):
⟨x²⟩ = ½ + η, ⟨x⁴⟩ = ¾ + 3η ⇒ Var(s²) = (⟨x⁴⟩ − ⟨x²⟩²)/N; Schätzer η̂ = ⟨x²⟩_A·(½/⟨x²⟩_V) − ½ mit
σ²(η̂) = (⟨x⁴⟩ − ⟨x²⟩²)/N_A + ⟨x²⟩²·2/N_V. Für W(0,0) aus der EM-Rekonstruktion gilt σ ≈ c_W/√N_A
(c_W aus Bootstrap, :func:`w00_coefficient`).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

VAC = 0.5


# ------------------------------------------------------------------------------------------- Aufbau
@dataclass
class PlanSpec:
    T_A: float = 60.0            # s je A-Lauf (Herald-Trigger)
    T_B: float = 30.0            # s je B-Lauf (freilaufend)
    n_cycles: int = 10           # A-B-Zyklen (zusätzlich ein abschließender A-Lauf)
    T_KV: float = 15.0           # s je K-V-Lauf
    kv_every: int = 3            # K-V nach jedem kv_every-ten Zyklus (0 = nur Anfang und Ende)
    T_KH: float = 60.0           # s K-H am Sitzungsanfang
    kh_end: bool = False         # zweiter K-H-Lauf am Ende
    rate_B: float = 2000.0       # 1/s interne periodische Trigger in B
    rate_KV: float = 4000.0      # 1/s interne periodische Trigger in K-V

    def to_dict(self) -> dict:
        return asdict(self)


def make_plan(spec: PlanSpec | dict | None = None, **kw) -> list[dict]:
    """Blockliste (für ``SimConfig.blocks``) eines Laufplans."""
    s = spec if isinstance(spec, PlanSpec) else PlanSpec(**{**(spec or {}), **kw})
    if s.n_cycles < 1 or min(s.T_A, s.T_B, s.T_KV) <= 0 or s.T_KH < 0 or s.kv_every < 0:
        raise ValueError("Laufplan: n_cycles ≥ 1, Dauern > 0")
    kv = lambda i: dict(id=f"kv_{i:02d}", kind="vacuum", duration_s=s.T_KV, periodic_rate=s.rate_KV)
    b = [kv(0)]
    if s.T_KH > 0:
        b.append(dict(id="kh_start", kind="herald_blocked", duration_s=s.T_KH))
    for i in range(1, s.n_cycles + 1):
        b.append(dict(id=f"a_{i:02d}", kind="heralded", duration_s=s.T_A))
        b.append(dict(id=f"b_{i:02d}", kind="free_running", duration_s=s.T_B, periodic_rate=s.rate_B))
        if s.kv_every and i % s.kv_every == 0 and i < s.n_cycles:
            b.append(kv(i))
    b.append(dict(id=f"a_{s.n_cycles + 1:02d}", kind="heralded", duration_s=s.T_A))
    b.append(dict(kv(99), id="kv_end"))
    if s.kh_end and s.T_KH > 0:
        b.append(dict(id="kh_end", kind="herald_blocked", duration_s=s.T_KH))
    return b


def timeline(blocks: list[dict], gap_s: float = 0.5) -> list[dict]:
    """Start, Mitte und Ende (s ab Sitzungsbeginn) je Lauf."""
    t, out = 0.0, []
    for b in blocks:
        T = float(b["duration_s"])
        out.append(dict(b, t_start=t, t_mid=t + T / 2, t_end=t + T))
        t += T + gap_s
    return out


# ------------------------------------------------------------------------------------------- Statistik
def accepted_rate(R: float, record_length: int = 5000, pretrigger: int = 2000, fs: float = 2.5e9,
                  rearm: float = 20e-9) -> float:
    """Rate angenommener Records bei Poisson-Triggern der Rate R (nicht verlängernde Totzeit: Rest des Records + Rearm)."""
    tau = (record_length - pretrigger) / fs + rearm
    return R / (1 + R * tau)


def moments_fock1(eta: float) -> tuple[float, float]:
    return VAC + eta, 0.75 + 3 * eta


def sigma_eta(eta: float, N_A: float, N_V: float) -> float:
    m2, m4 = moments_fock1(eta)
    return float(np.sqrt((m4 - m2 ** 2) / N_A + m2 ** 2 * 2 / N_V))


def n_for_sigma_eta(eta: float, sigma: float, N_V: float = np.inf) -> float:
    """benötigte A-Segmente für σ(η̂) ≤ sigma bei N_V Vakuumsegmenten (∞: nur A-Statistik)."""
    m2, m4 = moments_fock1(eta)
    rest = sigma ** 2 - (m2 ** 2 * 2 / N_V if np.isfinite(N_V) else 0.0)
    return float((m4 - m2 ** 2) / rest) if rest > 0 else float("inf")


def w00_coefficient(eta: float = 0.7, n: int = 20000, n_boot: int = 40, seed: int = 3) -> float:
    """c_W in σ(W(0,0)) ≈ c_W/√N (EM-Rekonstruktion wie :func:`hhd.sim.check.summarize`, Bootstrap)."""
    from .check import em_photon_number, wigner00
    rng = np.random.default_rng(seed)
    x = sample_fock1(rng, eta, n)
    bs = [wigner00(em_photon_number(x[rng.integers(0, n, n)], iters=150)) for _ in range(n_boot)]
    return float(np.std(bs) * np.sqrt(n))


def sample_fock1(rng: np.random.Generator, eta, n: int) -> np.ndarray:
    """phasengemittelte Quadraturen von |1⟩ nach Verlust η (Vakuumvarianz ½): x² von |1⟩ ~ Gamma(3/2, 1)."""
    x1 = np.sqrt(rng.gamma(1.5, 1.0, n)) * rng.choice((-1.0, 1.0), n)
    eta = np.broadcast_to(np.asarray(eta, float), (n,))
    return np.sqrt(eta) * x1 + np.sqrt(1 - eta) * rng.normal(0, np.sqrt(VAC), n)


def statistics(blocks: list[dict], R_herald: float = 1000.0, R_dark: float = 100.0, eta: float = 0.7,
               record_length: int = 5000, pretrigger: int = 2000, fs: float = 2.5e9, skip_b: int = 25,
               c_W: float = 0.66, gap_s: float = 0.5) -> dict:
    """Segmentzahlen, Zeiten, Datenmenge und statistische Fehler eines Laufplans."""
    R_acc = accepted_rate(R_herald + R_dark, record_length, pretrigger, fs)
    N = dict(heralded=0.0, free_running=0.0, vacuum=0.0, herald_blocked=0.0)
    T = dict(N)
    for b in blocks:
        k = b["kind"]
        rate = R_acc if k in ("heralded", "herald_blocked") else accepted_rate(b.get("periodic_rate", 2000.0),
                                                                               record_length, pretrigger, fs)
        N[k] = N.get(k, 0.0) + rate * b["duration_s"]
        T[k] = T.get(k, 0.0) + b["duration_s"]
    n_rec = sum(N.values())
    bytes_rec = 2 * record_length + 2 * (record_length // skip_b if skip_b else 0)
    t_total = sum(b["duration_s"] for b in blocks) + gap_s * (len(blocks) - 1)
    xi = R_herald / (R_herald + R_dark)
    eta_eff = eta * xi
    out = dict(R_accepted=R_acc, N=N, T=T, t_total_s=t_total, t_measure_s=sum(T.values()),
               duty_A=T["heralded"] / t_total, data_GB=n_rec * bytes_rec / 1e9,
               peak_rate_MBps=max(R_acc, max([b.get("periodic_rate", 0) for b in blocks] or [0])) * bytes_rec / 1e6)
    NA, NV, NB, NH = N["heralded"], N["vacuum"], N["free_running"], N["herald_blocked"]
    if NA and NV:
        m2, m4 = moments_fock1(eta_eff)
        out.update(sigma_eta=sigma_eta(eta_eff, NA, NV), sigma_eta_stat_A=float(np.sqrt((m4 - m2 ** 2) / NA)),
                   sigma_eta_vac=float(m2 * np.sqrt(2 / NV)), sigma_W00=float(c_W / np.sqrt(NA)),
                   sigma_W00_vac=float(2 / np.pi * m2 * np.sqrt(2 / NV)),
                   sigma_var_rel_A=float(np.sqrt((m4 - m2 ** 2) / NA) / m2))
    if NB and NV:
        out["sigma_nB"] = float(VAC * np.sqrt(2 / NB + 2 / NV))
    if NH:
        out["kh_excess_3sigma_rel"] = float(3 * np.sqrt(2 / NH))       # nachweisbare Zusatzvarianz (relativ)
    return out


# ------------------------------------------------------------------------------------------- Driftfehler
def _design(blocks: list[dict], mode: str, role: str, gap_s: float, n_sub: int = 8):
    """Zeiten und Gewichte des Normierungsfehlers e = Σ αᵢ·y(tᵢ) (y = relative Skalenabweichung).

    Für die Läufe der Rolle ``role`` (A: heralded, B: free_running) zählt +y(t) (zeitgemittelt über den Lauf),
    für die Kalibrierung −y(t_KV) (pooled: Mittel aller K-V; interpolated: lineare Interpolation in der Zeit;
    monitor: wie pooled, die LO-Drift kürzt sich, es bleibt die Drift der Monitor-Verstärkung)."""
    tl = timeline(blocks, gap_s)
    kind = {"A": "heralded", "B": "free_running"}[role]
    runs = [b for b in tl if b["kind"] == kind]
    kvs = [b for b in tl if b["kind"] == "vacuum"]
    if not runs or not kvs:
        raise ValueError("Laufplan braucht Läufe der Rolle und K-V")
    tk = np.array([b["t_mid"] for b in kvs]); wk = np.array([b["duration_s"] for b in kvs]); wk = wk / wk.sum()
    wr = np.array([b["duration_s"] for b in runs]); wr = wr / wr.sum()
    t, a = [], []
    for b, w in zip(runs, wr):
        ts = b["t_start"] + (np.arange(n_sub) + 0.5) * b["duration_s"] / n_sub
        t += list(ts); a += [w / n_sub] * n_sub
        if mode == "interpolated":
            for tt in ts:
                j = np.searchsorted(tk, tt)
                if j == 0 or j == len(tk):
                    t.append(tk[min(j, len(tk) - 1)]); a.append(-w / n_sub)
                else:
                    f = (tt - tk[j - 1]) / (tk[j] - tk[j - 1])
                    t += [tk[j - 1], tk[j]]; a += [-(1 - f) * w / n_sub, -f * w / n_sub]
    if mode in ("pooled", "monitor"):
        t += list(tk); a += list(-wk)
    return np.array(t), np.array(a)


def _err2(t, a, q=0.0, lin=0.0, amp=0.0, period=1.0) -> float:
    """E[e²] für e = Σ aᵢ y(tᵢ): Irrfahrt (Cov = q²·min), lineare Drift, Sinus mit zufälliger Phase."""
    e2 = q ** 2 * float(a @ np.minimum.outer(t, t) @ a) if q else 0.0
    e2 += (lin * float(a @ t)) ** 2
    if amp:
        z = np.sum(a * np.exp(2j * np.pi * t / period))
        e2 += 0.5 * amp ** 2 * abs(z) ** 2
    return float(e2)


def drift_error(blocks: list[dict], drift: dict, mode: str = "interpolated", eta: float = 0.7,
                gap_s: float = 0.5, n_sub: int = 8) -> dict:
    """Erwarteter Driftfehler (rms) von η̂ (A-Läufe), n̂_B und Δ = n̂_A − n̂_B je Normierung.

    ``drift``: lo_rw_per_sqrt_s, lo_rel_per_s, lo_sin_amp, lo_sin_period_s, gain_rw_per_sqrt_s (Monitor).
    Die relative Skalenabweichung der Varianz ist y = δP/P (pooled, interpolated) bzw. y = δg/g (monitor);
    sie wirkt auf n̂ mit dem Faktor ⟨x²⟩ (A: ½ + η, B: ½). Die Effizienzdrift ist keine Normierungs-, sondern
    eine physikalische Änderung (zeitgemittelter Wert) und erscheint im A-B-A-Indikator, nicht hier.
    """
    out = {}
    for role, m2 in (("A", VAC + eta), ("B", VAC)):
        t, a = _design(blocks, mode, role, gap_s, n_sub)
        if mode == "monitor":
            e2 = _err2(t, a, q=drift.get("gain_rw_per_sqrt_s", 0.0))
        else:
            e2 = _err2(t, a, q=drift.get("lo_rw_per_sqrt_s", 0.0), lin=drift.get("lo_rel_per_s", 0.0),
                       amp=drift.get("lo_sin_amp", 0.0), period=drift.get("lo_sin_period_s", 1200.0))
        out[role] = float(m2 * np.sqrt(e2))
    # Δ: gemeinsame Kalibrierung kürzt sich teilweise – direkt aus der Differenz der Designs
    tA, aA = _design(blocks, mode, "A", gap_s, n_sub); tB, aB = _design(blocks, mode, "B", gap_s, n_sub)
    t = np.concatenate([tA, tB]); a = np.concatenate([(VAC + eta) * aA, -VAC * aB])
    if mode == "monitor":
        e2 = _err2(t, a, q=drift.get("gain_rw_per_sqrt_s", 0.0))
    else:
        e2 = _err2(t, a, q=drift.get("lo_rw_per_sqrt_s", 0.0), lin=drift.get("lo_rel_per_s", 0.0),
                   amp=drift.get("lo_sin_amp", 0.0), period=drift.get("lo_sin_period_s", 1200.0))
    out["delta"] = float(np.sqrt(e2))
    # statistischer Fehler der Kalibrierung mit diesen Gewichten: Var(relative Vakuumvarianz je K-V) = 2/N_k
    tl = timeline(blocks, gap_s)
    kv = [b for b in tl if b["kind"] == "vacuum"]
    tk = np.array([b["t_mid"] for b in kv])
    Nk = np.array([accepted_rate(b.get("periodic_rate", 4000.0)) * b["duration_s"] for b in kv])
    for role, m2 in (("A", VAC + eta), ("B", VAC)):
        t, a = _design(blocks, mode, role, gap_s, n_sub)
        W = np.array([-a[np.isclose(t, x)].sum() for x in tk]) if mode == "interpolated" else Nk / Nk.sum()
        W = W / W.sum()
        out[f"cal_stat_{role}"] = float(m2 * np.sqrt(np.sum(W ** 2 * 2 / Nk)))
    return out


# ------------------------------------------------------------------------------------------- Anpassungsregel
def kv_spacing_max(noise: dict, budget: float, eta: float = 0.7, mode: str = "interpolated", T_A: float = 60.0,
                   T_B: float = 30.0, L_hi: float = 4 * 3600.0) -> float:
    """größter K-V-Abstand L (s), bis zu dem der Driftfehler von η̂ (:func:`drift_error`, A-Läufe) ≤ budget bleibt.

    Gerechnet für **ein** K-V-Intervall (K-V nur am Anfang und Ende, ungünstigster Fall ohne Mittelung über
    mehrere Intervalle) mit A-B-Zyklen T_A + T_B; die Abstände werden von klein
    nach groß abgetastet und beim ersten Überschreiten abgebrochen (periodische Drift ist nicht monoton in L).
    Rückgabe ``L_hi``, falls nie überschritten; 0, falls schon ein Zyklus zu lang ist."""
    drift = _noise_to_drift(noise)
    cyc = T_A + T_B + 1.0
    ks = np.unique(np.round(np.geomspace(1, max(1.0, L_hi / cyc), 18)).astype(int))
    last = 0.0
    for k in ks:
        bl = make_plan(PlanSpec(T_A=T_A, T_B=T_B, n_cycles=int(k), kv_every=0, T_KV=10.0, T_KH=0.0))
        if drift_error(bl, drift, mode, eta, n_sub=2)["A"] > budget:
            return float(last)
        last = float(k * cyc)
    return float(L_hi)


def _noise_to_drift(noise: dict) -> dict:
    return dict(lo_rw_per_sqrt_s=noise.get("q_rw", 0.0), lo_rel_per_s=noise.get("lin", 0.0),
                lo_sin_amp=noise.get("sin_amp", 0.0), lo_sin_period_s=noise.get("sin_period_s", 1200.0),
                gain_rw_per_sqrt_s=noise.get("gain_rw", 0.0))


def adapt_plan(noise: dict, R_herald: float = 1000.0, sigma_eta_target: float = 0.005, sigma_W00_target: float = 0.002,
               drift_share: float = 1 / 3, eta: float = 0.7, N_V_min: float = 50000.0, rate_KV: float = 10000.0,
               rate_B: float | None = None, n_cycles_min: int = 3, T_A_min: float = 10.0, T_A_max: float = 300.0,
               file_GB_max: float = 4.0, record_length: int = 5000, mode: str = "interpolated", T_KH: float = 60.0,
               c_W: float = 0.66) -> dict:
    """Anpassungsregel nach der Stabilitätsmessung (AP14): Laufplan aus Rauschmodell, Herald-Rate und Zielen.

    ``noise``: Ergebnis von :func:`hhd.sim.drift.fit_noise_model` der relativen Vakuumskala bzw. LO-Leistung
    (``q_rw`` 1/√s, ``lin`` 1/s), optional ``sin_amp``, ``sin_period_s`` (Spektrum) und ``gain_rw`` (Monitor).

    1. A-Segmente: N_A = max(N für σ_stat(η) ≤ σ·√(1−s)·…, (c_W/σ_W)²), s = ``drift_share``;
    2. Vakuumsegmente: N_V aus dem Vakuumanteil von σ(η) (mindestens ``N_V_min``);
    3. K-V-Abstand L ≤ :func:`kv_spacing_max` für das Driftbudget σ·√s;
    4. A-Lauf T_A = N_A/(R·(n+1)) mit n ≥ ``n_cycles_min`` Zyklen, begrenzt durch T_A_min/T_A_max,
       L/2 und die Dateigröße je Lauf; B-Lauf T_B = T_A/2;
    5. K-V-Läufe gleich lang, zusammen N_V.
    """
    R_acc = accepted_rate(R_herald, record_length=record_length)
    eta_eff = eta * R_herald / (R_herald + 20.0)
    m2, m4 = moments_fock1(eta_eff)
    s_stat = sigma_eta_target * np.sqrt(1 - drift_share)
    s_drift = sigma_eta_target * np.sqrt(drift_share)
    # Aufteilung des statistischen Anteils: A und Vakuum je zur Hälfte (Varianz)
    N_V = max(N_V_min, m2 ** 2 * 2 / (0.5 * s_stat ** 2))
    N_A = max((m4 - m2 ** 2) / (0.5 * s_stat ** 2), (c_W / sigma_W00_target) ** 2)
    T_A_total = N_A / R_acc
    bytes_rec = 2 * record_length * 1.04
    T_file = file_GB_max * 1e9 / (R_acc * bytes_rec)
    L_max = kv_spacing_max(noise, s_drift, eta_eff, mode)
    T_A = float(np.clip(T_A_total / (n_cycles_min + 1), T_A_min, min(T_A_max, T_file, max(T_A_min, L_max / 2))))
    n_cycles = max(n_cycles_min, int(np.ceil(T_A_total / T_A)) - 1)
    T_B = max(5.0, T_A / 2)
    cyc = T_A + T_B + 1.0
    kv_every = 0 if L_max >= (n_cycles + 1) * cyc else max(1, int(L_max // cyc))
    n_kv = 2 + (0 if kv_every == 0 else (n_cycles - 1) // kv_every)
    rate_KV_eff = accepted_rate(rate_KV, record_length=record_length)
    T_KV = max(5.0, float(np.ceil(N_V / n_kv / rate_KV_eff)))
    spec = PlanSpec(T_A=round(T_A, 1), T_B=round(T_B, 1), n_cycles=n_cycles, T_KV=T_KV, kv_every=kv_every,
                    T_KH=T_KH, rate_B=float(rate_B if rate_B is not None else min(R_herald, rate_KV)), rate_KV=rate_KV)
    bl = make_plan(spec)
    st = statistics(bl, R_herald=R_herald, eta=eta, record_length=record_length,
                    pretrigger=min(2000, record_length * 2 // 5) // 8 * 8, c_W=c_W)
    de = drift_error(bl, _noise_to_drift(noise), mode, eta_eff)
    tot = float(np.sqrt(st["sigma_eta_stat_A"] ** 2 + de["cal_stat_A"] ** 2 + de["A"] ** 2))
    if L_max < 2 * (T_A_min + max(5.0, T_A_min / 2) + 1.0) and mode != "monitor":
        hint = "Drift zu schnell für K-V-Zwischenläufe: LO-Monitor-Normierung (mode='monitor') oder Stabilisierung"
    elif kv_every:
        hint = f"K-V nach je {kv_every} Zyklen (Abstand ≤ {L_max / 60:.1f} min)"
    else:
        hint = "K-V nur am Anfang und Ende der Sitzung ausreichend"
    return dict(spec=spec.to_dict(), N_A_required=float(N_A), N_V_required=float(N_V), L_max_s=L_max, hint=hint,
                statistics=st, drift_error=de, sigma_eta_total=tot, sigma_W00=float(c_W / np.sqrt(st["N"]["heralded"])),
                feasible=bool(tot <= sigma_eta_target * 1.05 and de["A"] <= s_drift * 1.05), mode=mode)
