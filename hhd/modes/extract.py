"""Quadraturen aus Records (Kanal A) an der Modenposition aus der Synchronisation.

Modenposition je Record (Digitizer-Zeit):

* zugeordnete Herald-Records: c = h_D + Δ_Lauf (abgebildeter Herald + Feinkalibrierung);
* alle anderen Records (K-V, Lauf B, Zufalls-/Zustandstrigger, verwaist): c = T_trig + Δ_T – dieselbe Lage
  relativ zum Trigger wie die Herald-Mode (gleiches Fenster, gleiche Elektronik).

Vorlage f: doppelt-exponentiell √(γ/f_s)·e^(−γ|t|) oder Gauß, auf Σ f² = 1 normiert (unendlicher Träger).
**Bandbreitenmodell** (Option ``first_order``): die Analogbandbreite wirkt wie ein Tiefpass 1. Ordnung H auf
das Signal; die Mode im Record ist dann H·f. Die angepasste Vorlage ist Hᵀ·(H f)/‖H f‖ ≈ H f; verwendet wird
H f (normiert). Bei 1 GHz und 1/γ = 40 ns ist die Wirkung < 10⁻⁴ (wird im Bericht ausgewiesen), bei Gauß-Moden
weniger ns Breite aber nicht.

Fraktionale Positionen: Vorlagen für 1/32-Sample-Raster vorberechnet, Fenster ±10/γ (doppelt-exp.) bzw. ±6σ.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import signal as _sig

N_FRAC = 32


MODE_SHAPES = ("double_exponential", "gauss")


def resolve_mode_shape(shape: str, device: dict | None = None) -> str:
    """``auto``: Nenn-Modenform aus den Geräteattributen der Sitzung (``device.mode_shape``, vom Laserbetrieb
    abgeleitet: CW → double_exponential, Puls → gauss); ältere Sitzungen ohne Attribut → double_exponential."""
    if shape != "auto":
        return shape
    v = (device or {}).get("mode_shape")
    v = v.decode() if isinstance(v, bytes) else (str(v) if v is not None else None)
    return v if v in MODE_SHAPES else "double_exponential"


def laser_info(device: dict | None) -> dict:
    """Laserbetrieb der Sitzung für das Ergebnis: cw | pulsed (ältere Sitzungen ohne Attribut: cw)."""
    d = device or {}
    m = d.get("laser_mode", "cw")
    m = m.decode() if isinstance(m, bytes) else str(m)
    out = dict(mode=m if m in ("cw", "pulsed") else "cw", mode_shape=resolve_mode_shape("auto", d))
    if out["mode"] == "pulsed" and d.get("pulse_rate_Hz") is not None:
        out["pulse_rate_Hz"] = float(d["pulse_rate_Hz"])
    return out


@dataclass
class ModeSpec:
    shape: str = "double_exponential"
    gamma: float = 1 / 40e-9
    fs: float = 2.5e9
    bandwidth_Hz: float | None = None          # None = ohne Bandbreitenmodell
    custom: np.ndarray | None = None           # datengetriebene Mode (Samples, zentriert, Raster 1/f_s)

    @property
    def half(self) -> int:
        if self.custom is not None:
            return (len(self.custom) - 1) // 2
        h = (10.0 if self.shape == "double_exponential" else 6.0) / self.gamma
        return int(np.ceil(h * self.fs))

    def to_dict(self) -> dict:
        return dict(shape="custom" if self.custom is not None else self.shape, gamma=self.gamma, fs=self.fs,
                    bandwidth_Hz=self.bandwidth_Hz, half_samples=self.half)


def mode_function(spec: ModeSpec, u: np.ndarray) -> np.ndarray:
    """f an Sample-Offsets u (in Samples, Zentrum 0), unnormiert."""
    t = u / spec.fs
    if spec.custom is not None:
        h = (len(spec.custom) - 1) // 2
        return np.interp(u, np.arange(-h, h + 1), spec.custom, left=0.0, right=0.0)
    if spec.shape == "double_exponential":
        return np.exp(-spec.gamma * np.abs(t))
    if spec.shape == "gauss":
        return np.exp(-0.5 * (t * spec.gamma) ** 2)
    raise ValueError(spec.shape)


@dataclass
class TemplateBank:
    spec: ModeSpec
    T: np.ndarray            # (N_FRAC, 2·half+1): Vorlage für Bruchteil j/N_FRAC
    bandwidth_effect: float = 0.0   # 1 − |⟨f, Hf⟩|² (normiert)

    @property
    def half(self) -> int:
        return (self.T.shape[1] - 1) // 2


def template_bank(spec: ModeSpec) -> TemplateBank:
    h = spec.half
    pad = 64
    k = np.arange(-h - pad, h + pad + 1)
    rows, eff = [], []
    filt = None
    if spec.bandwidth_Hz:
        wn = spec.bandwidth_Hz / (spec.fs / 2)
        if wn < 1:
            filt = _sig.butter(1, wn)
    for j in range(N_FRAC):
        f = mode_function(spec, k - j / N_FRAC)
        f0 = f / np.linalg.norm(f)
        if filt is not None:
            g = _sig.lfilter(*filt, f0)
            g = g / np.linalg.norm(g)
            eff.append(1 - float(f0 @ g) ** 2)
            f0 = g
        rows.append(f0[pad:pad + 2 * h + 1])
    return TemplateBank(spec, np.array(rows), float(np.mean(eff)) if eff else 0.0)


def project_records(data: np.ndarray, pos: np.ndarray, bank: TemplateBank, offset: float = 0.0
                    ) -> tuple[np.ndarray, np.ndarray]:
    """q_j = Σ_k T(k − pos_j)·(y_jk − offset). ``pos``: Modenzentrum in Samples (Record-Koordinaten).
    Rückgabe (q, Abdeckung = Σ T² innerhalb des Records)."""
    n, L = data.shape
    h = bank.half
    pos = np.asarray(pos, float)
    i0 = np.floor(pos).astype(np.int64)
    fr = np.clip(np.rint((pos - i0) * N_FRAC).astype(np.int64), 0, N_FRAC)
    i0 = i0 + (fr == N_FRAC); fr[fr == N_FRAC] = 0
    T = bank.T[fr]                                  # (n, 2h+1)
    idx = i0[:, None] + np.arange(-h, h + 1)[None, :]
    ok = (idx >= 0) & (idx < L)
    Tm = np.where(ok, T, 0.0)
    y = np.take_along_axis(data, np.clip(idx, 0, L - 1), axis=1).astype(np.float64) - offset
    return np.einsum("ij,ij->i", Tm, y), np.einsum("ij,ij->i", Tm, Tm)


@dataclass
class RunQuadratures:
    """Rohquadraturen eines Laufs (LSB-Einheiten, ADC-Nullpunkt abgezogen) und Begleitgrößen je Record."""
    block: str
    dataset_type: str
    q: np.ndarray
    coverage: np.ndarray
    seg_class: np.ndarray
    centre_D: np.ndarray
    t_session: np.ndarray            # Sitzungszeit des Triggers (s)
    lo_b: np.ndarray                 # Mittelwert Kanal B je Record (LSB, ohne Dunkelabzug) oder NaN
    clean: np.ndarray                # zugeordnet, ohne sekundäre Moden, ohne Überlappung, Abdeckung ≥ 0,99
    info: dict = field(default_factory=dict)


def record_means_b(run, chunk: int = 4096) -> np.ndarray:
    """Mittelwert Kanal B (LO-Monitor) je Record. Live mit ``channel_b_trigger`` = SYNC liegt Kanal B nur zu den
    Markerzeiten vor (``markers/data_b``); dann wird auf die Triggerzeiten interpoliert (LO-Drift ≪ 1 kHz)."""
    if run.n_records and not run.has_data_b and getattr(run, "has_marker_b", False):
        tm, mb = run.marker_b_means()
        if len(tm) >= 1:
            return np.interp(run.trigger_D(), tm, mb) if len(tm) > 1 else np.full(run.n_records, mb[0])
    if not run.has_data_b or run.n_records == 0:
        return np.full(run.n_records, np.nan)
    out = np.empty(run.n_records)
    for i0, blk in run.iter_records(chunk=chunk, channel="B"):
        out[i0:i0 + len(blk)] = blk.mean(axis=1)
    return out


def extract_run(run, centre_D: np.ndarray, bank: TemplateBank, offset: float, seg_class: np.ndarray,
                clean_mask: np.ndarray, t_session: np.ndarray, chunk: int = 2048) -> RunQuadratures:
    first = run.first_sample_D()
    pos = (centre_D - first) * run.fs
    q = np.empty(run.n_records); cov = np.empty(run.n_records)
    for i0, blk in run.iter_records(chunk=chunk):
        sl = slice(i0, i0 + len(blk))
        q[sl], cov[sl] = project_records(blk, pos[sl], bank, offset)
    clean = clean_mask & (cov >= 0.99)
    return RunQuadratures(run.block_id, run.dataset_type, q, cov, seg_class, centre_D, t_session,
                          record_means_b(run), clean, dict(n_low_coverage=int(np.sum(cov < 0.99))))


def adc_offset(runs, max_records: int = 4000) -> float:
    """ADC-Nullpunkt aus K-V-Records (Mittel aller Samples)."""
    s = 0.0; n = 0
    for r in runs:
        for _, blk in r.iter_records(chunk=1024):
            s += float(blk.sum(dtype=np.float64)); n += blk.size
            if n >= max_records * blk.shape[1]:
                return s / n
    return s / n if n else 0.0
