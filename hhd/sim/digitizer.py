"""Gerätemodell Teledyne SP Devices ADQ32-PCIe (Strom 2), Architektur A (getriggerte Records).

Herstellerangaben **[Spez.]**:

* Abtastrate 2,5 bzw. 2 GS/s je Kanal (2-Kanal-Modus), 5 bzw. 4 GS/s (1-Kanal-Modus), 12 bit
  (Datenblatt 20-2378);
* TRIG-Zeitauflösung 50 ps, SYNC 3,2 ns bei 2,5 GS/s, linear mit der Abtastrate skalierend; TRIG max. 10 MHz;
  Rearm-Zeit max. 20 ns; Pretrigger 2-Kanal in Schritten von 8 bis 16 360 Samples (Datenblatt 20-2378);
* Record-Header: ``timestamp`` (Trigger auf dem Zeitgitter), ``record_start`` (erstes Sample relativ zum
  Trigger), Einheit ``time_unit``; t_x[0] = timestamp + record_start (User Guide 21-2539, Kap. 9.3);
* Sample Skip verwirft Samples ohne Tiefpass, je Kanal einstellbar (User Guide, Kap. 5.2); Recordlänge,
  Horizontal-Offset und Triggerquelle sind je Kanal einstellbar (User Guide, ADQDataAcquisitionParametersChannel);
* Kanal B: LO-Leistungsmonitor (Photodiode), gleicher Trigger wie Kanal A, mit eigenem Sample Skip;
* Pulsgenerator (User Guide, Kap. 7.2): ein Puls je Ereignis der gewählten Ereignisquelle (TRIG-Port bzw.
  periodisch), Ausgabe an einem Port (hier GPIO); Zeitraster 32 Samples der Basisrate (6,4 ns bei 5 GHz).

Modellannahmen: ``time_unit`` = 1/(f_ADC·8) (nicht dokumentiert); periodische interne Trigger haben
Sample-Auflösung; der Record-Header meldet den auslösenden Eingang nicht (konfigurierbar).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import signal as _sig

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


def frontend_filter(cfg: SimConfig):
    """Analogbandbreite als digitaler Tiefpass 1. Ordnung (−3 dB bei der Bandbreitenoption) [Annahme].

    Rückgabe (b, a) oder None, wenn die Bandbreite über der Nyquist-Frequenz liegt oder das Filter aus ist.
    Auf die Mode (1/γ = 40 ns, Bandbreite einige MHz) wirkt das Filter praktisch nicht; es färbt nur das
    Rauschen je Sample.
    """
    if cfg.digitizer.frontend_filter == "none":
        return None
    wn = cfg.bandwidth / (cfg.fs / 2)
    if not wn < 1:
        return None
    return _sig.butter(1, wn)


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
    data_b: np.ndarray | None = None  # Kanal B (LO-Monitor), int16 (n, L_B)
    lo_rel_true: np.ndarray | None = None  # wahre relative LO-Leistung je Record
    ramp_raw: np.ndarray | None = None     # Hilfs-ADC: Rampenspannung des Piezos je Record (LSB), ab 0.15


class ADQ32:
    def __init__(self, cfg: SimConfig, clock: DigitizerClock):
        self.cfg, self.clk = cfg, clock
        d = cfg.digitizer
        self.fs = cfg.fs
        self.tu = cfg.time_unit
        self.tu_per_sample = int(round((1 / self.fs) / self.tu))
        self.L, self.pre = d.record_length, d.pretrigger
        self.lim = 2 ** (d.adc_bits - 1) - 1
        self.filt = frontend_filter(cfg)
        if self.filt is not None:
            self.filt_zi = _sig.lfilter_zi(*self.filt)

    def device_attrs(self) -> dict:
        c, d = self.cfg, self.cfg.digitizer
        return dict(model=f"{d.model}-{d.form_factor} (SIMULATED)", form_factor=d.form_factor,
                    clock_option=d.clock_option, channel_mode=d.channel_mode, f_adc_Hz=c.f_adc,
                    skip_factor=d.skip_factor, sampling_rate_Hz=self.fs, time_unit_s=self.tu,
                    sampling_period_tu=self.tu_per_sample, adc_bits=d.adc_bits, record_length=self.L,
                    horizontal_offset=-self.pre, trig_resolution_s=c.trig_res, sync_resolution_s=c.sync_res,
                    reference_clock="external 10 MHz" if c.clock.shared_10MHz else "internal",
                    bandwidth_option=d.bandwidth_option, analog_bandwidth_Hz=c.bandwidth,
                    header_reports_trigger_source=d.header_reports_trigger_source, channel="A",
                    channel_a_role="homodyne", channel_b_role="lo_monitor" if c.lo_b_enabled else "off",
                    channel_b_skip_factor=c.lo_monitor.skip_factor if c.lo_b_enabled else 0,
                    channel_b_record_length=c.lo_b_record_length if c.lo_b_enabled else 0,
                    channel_b_horizontal_offset=-c.lo_b_pretrigger if c.lo_b_enabled else 0,
                    lo_monitor_dark_lsb=c.lo_monitor.dark_lsb, lo_monitor_lsb_nominal=c.lo_monitor.lsb_nominal,
                    trigger_copy_port="GPIO", pulse_generator_step_s=c.pulse_gen_step,
                    wavelength_nm=c.optics.wavelength_nm, herald_detector=c.optics.herald_detector,
                    lo_monitor_pd=c.lo_monitor.pd_model, lo_monitor_attenuator_dB=c.lo_monitor.attenuator_dB,
                    lo_monitor_dc_offset_V=c.lo_monitor.dc_offset_V, **laser_attrs(c))

    def periodic_grid(self, t0: float, duration: float, rate: float) -> np.ndarray:
        """Digitizer-Zeiten der internen periodischen Trigger im Lauf (Sample-Auflösung [Annahme])."""
        if rate <= 0:
            return np.empty(0)
        TD0, TD1 = self.clk(t0), self.clk(t0 + duration)
        tp = np.arange(np.ceil(TD0 * rate), np.floor(TD1 * rate)) / rate
        return np.ceil(tp * self.fs) / self.fs

    def trigger_copy(self, rng: np.random.Generator, t0: float, duration: float, trig_true: np.ndarray,
                     periodic_rate: float) -> np.ndarray:
        """Kopie der Triggerereignisse über einen Pulsgenerator am GPIO-Port → wahre Zeit am GPIO-Ausgang.

        Der Pulsgenerator gibt **jedes** Ereignis der Quelle aus (nicht nur angenommene Trigger) [Spez., UG 7.2];
        ob er sich auf angenommene Trigger beschränken lässt, ist offen [unsicher]. Zeitraster 6,4 ns.
        """
        c, d, clk = self.cfg, self.cfg.digitizer, self.clk
        TD = np.concatenate([clk(np.asarray(trig_true, float) + rng.normal(0, d.trig_jitter, len(trig_true))),
                             self.periodic_grid(t0, duration, periodic_rate)])
        TD = TD[(TD >= clk(t0)) & (TD <= clk(t0 + duration))]
        q = c.pulse_gen_step
        TDo = np.ceil(TD / q - 1e-9) * q + d.trigger_copy_latency
        return np.sort(clk.inverse(TDo))

    # ------------------------------------------------------------------ Trigger → Records
    def acquire(self, rng: np.random.Generator, t0: float, duration: float, trig: TriggerInput,
                periodic_rate: float, mode_centre_true: np.ndarray, mode_x: np.ndarray,
                sync_true: np.ndarray, crosstalk_true: np.ndarray, crosstalk_amp: float,
                lo_on: bool = True, lo_scale_fn=None, monitor_gain_fn=None) -> DigitizerRecording:
        """``lo_scale_fn(t_true)``: relative LO-Leistung (Drift); das optische Signal skaliert mit √P_LO.

        ``monitor_gain_fn(t_true)``: relative Verstärkung des LO-Monitors an Kanal B (Drift des Abgriffs)."""
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
            tp = self.periodic_grid(t0, duration, periodic_rate)
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
        t_rec_true = t0 + (ts_rec * tu - TD_start) / (1 + clk.eps)
        if lo_scale_fn is not None and n:
            lo_amp = np.sqrt(np.asarray(lo_scale_fn(t_rec_true), float) * np.ones(n))
        else:
            lo_amp = np.ones(n)
        lo_rel = lo_amp ** 2 if lo_on else np.zeros(n)
        data_b = None
        if c.lo_b_enabled:                               # Kanal B: Photodiode am LO (Mittelwert ∝ P_LO)
            lm = c.lo_monitor
            Lb = c.lo_b_record_length
            p_rec = lo_rel * (1.0 + rng.normal(0, lm.rin_rel, n))
            if monitor_gain_fn is not None and n:
                p_rec = p_rec * np.asarray(monitor_gain_fn(t_rec_true), float)
            yb = lm.dark_lsb + lm.lsb_nominal * p_rec[:, None] + rng.normal(0, lm.noise_lsb, (n, Lb))
            data_b = np.clip(np.round(yb), -self.lim - 1, self.lim).astype(np.int16).reshape(n, Lb)
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
            y = lo_amp[j] * w + rng.normal(0, sig_el, L)
            if self.filt is not None:
                y, _ = _sig.lfilter(*self.filt, y, zi=self.filt_zi * y[0])
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
                                  kind_all[acc], idx_all[acc], ttrue_all[acc], ts_m, okm, n_in, cand_acc,
                                  data_b=data_b, lo_rel_true=lo_rel)


def laser_attrs(cfg) -> dict:
    """Laserbetrieb und Nenn-Modenform für die Geräteattribute (Gruppe ``device``): die Auswertung wählt damit die
    Modenform (``mode_shape="auto"``); bei Puls zusätzlich die Pulsfolgefrequenz."""
    s = cfg.source
    out = dict(laser_mode=s.laser_mode, mode_shape=s.mode_shape)
    if s.laser_mode == "pulsed":
        out["pulse_rate_Hz"] = float(s.pulse_rate_Hz)
    return out
