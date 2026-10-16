"""Gerätemodell Teledyne SP Devices ADQ32-PCIe (Strom 2), Architektur A (getriggerte Records).

Herstellerangaben **[Spez.]**:

* Abtastrate 2,5 bzw. 2 GS/s je Kanal (2-Kanal-Modus), 5 bzw. 4 GS/s (1-Kanal-Modus), 12 bit
  (Datenblatt 20-2378);
* TRIG-Zeitauflösung 50 ps, SYNC 3,2 ns bei 2,5 GS/s, linear mit der Abtastrate skalierend; TRIG max. 10 MHz;
  Rearm-Zeit max. 20 ns; Pretrigger 2-Kanal in Schritten von 8 bis 16 360 Samples (Datenblatt 20-2378);
* Record-Header: ``timestamp`` (Trigger auf dem Zeitgitter), ``record_start`` (erstes Sample relativ zum
  Trigger), Einheit ``time_unit``; t_x[0] = timestamp + record_start (User Guide 21-2539, Kap. 9.3);
* Sample Skip verwirft Samples ohne Tiefpass (User Guide, Kap. 5.2).

Modellannahmen: ``time_unit`` = 1/(f_ADC·8) (nicht dokumentiert); periodische interne Trigger haben
Sample-Auflösung; der Record-Header meldet den auslösenden Eingang nicht (konfigurierbar).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..io.schema import TRIGGER_SOURCES
from .clock import DigitizerClock
from .config import SimConfig
from .states import VAC_VAR

TRIG_HERALD, TRIG_RANDOM, TRIG_PERIODIC, TRIG_STATE = 0, 1, 2, 3
TRIG_KIND_NAMES = {TRIG_HERALD: "herald", TRIG_RANDOM: "random", TRIG_PERIODIC: "periodic", TRIG_STATE: "state"}


def mode_vector(shape: str, gamma: float, fs: float, tk: np.ndarray, centre: float) -> np.ndarray:
    """Diskrete, auf ‖·‖ = 1 (unendlicher Träger) normierte Modenfunktion an den Sample-Zeiten ``tk``."""
    d = tk - centre
    if shape == "double_exponential":
        return np.sqrt(gamma / fs) * np.exp(-gamma * np.abs(d))
    if shape == "gauss":
        s = 1.0 / gamma
        return np.exp(-d ** 2 / (2 * s ** 2)) / (np.pi ** 0.25 * np.sqrt(s * fs))
    raise ValueError(shape)


def mode_halfwidth(shape: str, gamma: float) -> float:
    return (10.0 if shape == "double_exponential" else 6.0) / gamma


@dataclass
class TriggerInput:
    """Triggerkandidaten: wahre Zeiten am Eingang (TRIG) bzw. Digitizer-Zeiten (periodisch)."""
    t_true: np.ndarray = field(default_factory=lambda: np.empty(0))
    kind: np.ndarray = field(default_factory=lambda: np.empty(0, np.int8))
    index: np.ndarray = field(default_factory=lambda: np.empty(0, np.int64))


@dataclass
class DigitizerRecording:
    data: np.ndarray
    timestamp: np.ndarray
    record_start: np.ndarray
    trigger_source: np.ndarray
    rec_kind: np.ndarray
    rec_index: np.ndarray
    rec_trigger_true: np.ndarray
    marker_ts: np.ndarray
    marker_recorded: np.ndarray
    n_modes_in_record: np.ndarray
    cand_accepted: np.ndarray        # je TRIG-Kandidat: Recordnummer oder −1


class ADQ32:
    def __init__(self, cfg: SimConfig, clock: DigitizerClock):
        self.cfg, self.clk = cfg, clock
        d = cfg.digitizer
        self.fs = cfg.fs
        self.tu = cfg.time_unit
        self.tu_per_sample = int(round((1 / self.fs) / self.tu))
        self.L, self.pre = d.record_length, d.pretrigger
        self.lim = 2 ** (d.adc_bits - 1) - 1

    def device_attrs(self) -> dict:
        c, d = self.cfg, self.cfg.digitizer
        return dict(model=f"{d.model}-{d.form_factor} (SIMULATED)", form_factor=d.form_factor,
                    clock_option=d.clock_option, channel_mode=d.channel_mode, f_adc_Hz=c.f_adc,
                    skip_factor=d.skip_factor, sampling_rate_Hz=self.fs, time_unit_s=self.tu,
                    sampling_period_tu=self.tu_per_sample, adc_bits=d.adc_bits, record_length=self.L,
                    horizontal_offset=-self.pre, trig_resolution_s=c.trig_res, sync_resolution_s=c.sync_res,
                    reference_clock="external 10 MHz" if c.clock.shared_10MHz else "internal",
                    header_reports_trigger_source=d.header_reports_trigger_source, channel="A")

    # ------------------------------------------------------------------ Trigger → Records
    def acquire(self, rng: np.random.Generator, t0: float, duration: float, trig: TriggerInput,
                periodic_rate: float, mode_centre_true: np.ndarray, mode_x: np.ndarray,
                sync_true: np.ndarray, crosstalk_true: np.ndarray, crosstalk_amp: float,
                lo_on: bool = True) -> DigitizerRecording:
        c, d, clk = self.cfg, self.cfg.digitizer, self.clk
        fs, tu, L, pre = self.fs, self.tu, self.L, self.pre
        # TRIG-Kandidaten (Herald, Zufall, Zustand) mit Eingangsjitter und Zeitauflösung
        TD = clk(trig.t_true + rng.normal(0, d.trig_jitter, len(trig.t_true)))
        ts_trig = np.floor(TD / c.trig_res + 1e-9) * c.trig_res
        kinds = [np.asarray(trig.kind, np.int8)]
        idx = [np.asarray(trig.index, np.int64)]
        tsl = [ts_trig]
        ttrue = [trig.t_true]
        if periodic_rate > 0:
            TD0, TD1 = clk(t0), clk(t0 + duration)
            tp = np.arange(np.ceil(TD0 * periodic_rate), np.floor(TD1 * periodic_rate)) / periodic_rate
            tp = np.ceil(tp * fs) / fs                                  # Sample-Auflösung [Annahme]
            tsl.append(tp); kinds.append(np.full(len(tp), TRIG_PERIODIC, np.int8))
            idx.append(np.arange(len(tp))); ttrue.append(np.full(len(tp), np.nan))
        ts_all = np.concatenate(tsl)
        kind_all = np.concatenate(kinds)
        idx_all = np.concatenate(idx)
        ttrue_all = np.concatenate(ttrue)
        ts_tu = np.rint(ts_all / tu).astype(np.int64)
        # Armierung: nur innerhalb des Blocks, Pretrigger-Daten vorhanden, Rearm
        TD_start, TD_end = clk(t0), clk(t0 + duration)
        post = (L - pre) / fs + d.rearm
        order = np.argsort(ts_tu, kind="stable")
        accepted = []
        busy = -np.inf
        for i in order:
            td = ts_tu[i] * tu
            if td < TD_start or td > TD_end - L / fs or td < pre / fs or td < busy:
                continue
            accepted.append(i)
            busy = td + post
        acc = np.asarray(accepted, np.int64)
        n = len(acc)
        cand_acc = -np.ones(len(trig.t_true), np.int64)
        is_trig = acc < len(trig.t_true)
        cand_acc[acc[is_trig]] = np.flatnonzero(is_trig)
        ts_rec = ts_tu[acc]
        s_trig = np.floor(ts_rec * tu * fs + 1e-9).astype(np.int64)
        s0 = s_trig - pre
        record_start = s0 * self.tu_per_sample - ts_rec
        # Wellenformen
        cD = clk(mode_centre_true) if len(mode_centre_true) else np.empty(0)
        o = np.argsort(cD); cDs = cD[o]
        hw = mode_halfwidth(c.source.mode_shape, c.source.gamma)
        sig_el = np.sqrt(VAC_VAR / c.clearance)
        xtD = np.sort(clk(crosstalk_true)) if len(crosstalk_true) and crosstalk_amp else np.empty(0)
        kk = np.arange(L)
        data = np.empty((n, L), np.int16)
        n_in = np.zeros(n, np.int32)
        for j in range(n):
            tk = (s0[j] + kk) / fs
            w = rng.normal(0, np.sqrt(VAC_VAR), L) if lo_on else np.zeros(L)
            if lo_on and len(cDs):
                lo_i = np.searchsorted(cDs, tk[0] - hw)
                hi_i = np.searchsorted(cDs, tk[-1] + hw)
                for m in o[lo_i:hi_i]:
                    g = mode_vector(c.source.mode_shape, c.source.gamma, fs, tk, cD[m])
                    gn = np.linalg.norm(g)
                    if gn < 1e-3:
                        continue
                    gh = g / gn
                    cap = min(gn, 1.0)
                    xm = cap * mode_x[m] + np.sqrt(max(0.0, 1 - cap ** 2)) * rng.normal(0, np.sqrt(VAC_VAR))
                    w = w - gh * (gh @ w) + gh * xm
                    n_in[j] += 1
            if len(xtD):
                a = np.searchsorted(xtD, tk[0] - 20 * d.crosstalk_tau)
                b = np.searchsorted(xtD, tk[-1])
                for tx in xtD[a:b]:
                    dt = tk - tx - d.crosstalk_delay
                    w = w + crosstalk_amp * np.sqrt(VAC_VAR) * np.where(dt >= 0, np.exp(-np.clip(dt, 0, None) / d.crosstalk_tau), 0.0)
            y = w + rng.normal(0, sig_el, L)
            data[j] = np.clip(np.round(y * d.lsb_per_unit + d.adc_offset), -self.lim - 1, self.lim)
        # Triggerquelle im Header
        if d.header_reports_trigger_source:
            src = np.where(kind_all[acc] == TRIG_PERIODIC, TRIGGER_SOURCES["PERIODIC"], TRIGGER_SOURCES["TRIG"])
        else:
            src = np.full(n, TRIGGER_SOURCES["UNKNOWN"])
        # SYNC-Marker
        TDm = clk(sync_true + rng.normal(0, d.trig_jitter, len(sync_true)))
        okm = (TDm >= TD_start) & (TDm <= TD_end) & (TDm >= 0) & (rng.random(len(sync_true)) >= d.p_miss_marker)
        ts_m = np.rint(np.floor(TDm[okm] / c.sync_res + 1e-9) * c.sync_res / tu).astype(np.int64)
        return DigitizerRecording(data, ts_rec, record_start.astype(np.int64), src.astype(np.uint8),
                                  kind_all[acc], idx_all[acc], ttrue_all[acc], ts_m, okm, n_in, cand_acc)
