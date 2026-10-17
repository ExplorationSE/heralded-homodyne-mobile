"""Simulation einer Messsitzung aus mehreren getrennten Messläufen (Messablauf laut Gesamtplan v1.5, AP18).

Struktur (ED-12): **Kampagne** → **Sitzung** (gemeinsame Kalibrierung ``calibration_id``) → **Läufe**
(Blöcke in ``blocks``; je Lauf eine Lauf-ID, eine eigene PTU-Datei der HydraHarp 500 und eine Gruppe in der
Digitizer-Datei). Eine Sitzung schreibt die PTU-Dateien (Strom 1), eine Digitizer-Datei im HDF5-Schema v1.1
(Strom 2), eine getrennte Wahrheitsdatei und das Manifest ``session.json``.

Standard ist die Folge K-V → K-H → A (Herald-Trigger) → B (freilaufender Trigger) → A → K-V; optional simuliert
``drift`` eine Drift von LO-Leistung und Effizienz über die Sitzung (Prüfung der Gegenmaßnahmen).

Kanalbelegung (Gesamtplan v1.5): HydraHarp 500 S – Sync-Eingang = Sync-Marker, CH1 Herald, CH2 Kopie der
ADQ32-Trigger (Pulsgenerator am GPIO), CH3 reserviert (zweiter Herald/PNR), CH4 Hilfstrigger (Zustands- bzw.
Zufallstrigger), M1 Laufmarke, M2 Phasenreferenz (Option). ADQ32 – Kanal A Homodyn, Kanal B LO-Monitor.

Laufarten (``BlockCfg.kind``) und Physik:

=================  ================  ======================  ===========================================
kind               dataset_type      Trigger am ADQ32        Signalmoden
=================  ================  ======================  ===========================================
heralded           heralded          Herald (+ Zufall, OR)   alle SPDC-Paare (geheraldet + ungeheraldet)
random             random_trigger    Zufall                  alle SPDC-Paare (unbedingter Zustand)
free_running       free_running      periodisch (intern)     alle SPDC-Paare (unbedingter Zustand)
herald_blocked     herald_blocked    Herald                  keine (Signal blockiert, K-H)
vacuum             vacuum            periodisch (intern)     keine (Signal blockiert, K-V)
electronic         electronic        periodisch (intern)     keine, LO aus (nur elektronisches Rauschen)
state              prepared_state    Zustandstrigger         Testzustand (kohärent, thermisch, Fock, Katze)
=================  ================  ======================  ===========================================
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from .. import __version__
from ..io import ptu as P
from ..io import schema as S
from .clock import DigitizerClock
from .config import BlockCfg, SimConfig
from .drift import DriftPath
from .digitizer import ADQ32, TRIG_HERALD, TRIG_RANDOM, TRIG_STATE, TriggerInput
from .harp import HydraHarp500, apply_dead_time
from .phase import PhaseModel
from .states import make_state

KIND_TO_DATASET = dict(heralded="heralded", random="random_trigger", free_running="free_running",
                       herald_blocked="herald_blocked", vacuum="vacuum", electronic="electronic", state="prepared_state")
KIND_TO_ROLE = dict(heralded="heralded", random="unheralded", free_running="unheralded", herald_blocked="control",
                    vacuum="control", electronic="control", state="test_state")
MODE_HERALDED, MODE_UNHERALDED, MODE_STATE = 0, 1, 2
MANIFEST = "session.json"


@dataclass
class BlockResult:
    cfg: BlockCfg
    index: int
    t0: float
    duration: float
    dataset_type: str
    run_id: str
    attrs: dict
    harp_records: np.ndarray            # T2-Records (uint32)
    harp_record_time_ps: np.ndarray
    digitizer: Any                      # DigitizerRecording
    truth: dict
    expected: dict
    warnings: list = field(default_factory=list)
    monitor: dict | None = None             # LO-Leistungsmonitor (t_s, lo_power_rel)


@dataclass
class RunResult:
    cfg: SimConfig
    blocks: list[BlockResult]
    device_attrs: dict
    clock: DigitizerClock
    calibration_id: str
    session_id: str = ""
    campaign_id: str = ""
    out_dir: Path | None = None
    files: dict = field(default_factory=dict)
    drift: Any = None                    # DriftPath (stochastische Drift) oder None


def _poisson_times(rng, rate, t0, T):
    n = rng.poisson(rate * T) if rate > 0 else 0
    return np.sort(rng.uniform(t0, t0 + T, n))


def lo_scale_fn(cfg: SimConfig, b: BlockCfg, path: DriftPath | None = None):
    """relative LO-Leistung als Funktion der wahren Zeit (Drift ab Sitzungsbeginn × Laufsprung).

    Ohne stochastische Drift (``path`` None oder rein linear) gilt das lineare Modell ``drift.lo_rel_per_s``.
    """
    if path is not None and path.stochastic:
        return lambda t: b.lo_scale * path.lo(t)
    t_s, a = cfg.session.start_s, cfg.drift.lo_rel_per_s
    return lambda t: b.lo_scale * (1.0 + a * (np.asarray(t, float) - t_s))


def eta_of_run(cfg: SimConfig, b: BlockCfg, t_mid: float, path: DriftPath | None = None) -> float:
    """Zustandseffizienz im Lauf (Drift zur Laufmitte × Laufsprung), begrenzt auf (0, 1]."""
    if path is not None and path.stochastic:
        f = float(path.eta(t_mid))
    else:
        f = 1.0 + cfg.drift.eta_rel_per_s * (t_mid - cfg.session.start_s)
    return float(np.clip(cfg.source.eta * b.eta_scale * f, 1e-6, 1.0))


def simulate_block(cfg: SimConfig, b: BlockCfg, index: int, t0: float, clk: DigitizerClock, adq: ADQ32,
                   harp: HydraHarp500, ss: np.random.SeedSequence, calibration_id: str, run_id: str = "",
                   drift_path: DriftPath | None = None) -> BlockResult:
    rs = [np.random.default_rng(s) for s in ss.spawn(8)]
    r_src, r_state, r_phase, r_harp, r_dig, r_mark, r_trig, r_misc = rs
    src, h, d, mk = cfg.source, cfg.harp, cfg.digitizer, cfg.markers
    T = b.duration_s
    kind = b.kind
    pcfg = cfg.phase_for(b)
    phase = PhaseModel(pcfg, t0, T)
    signal_blocked = kind in ("herald_blocked", "vacuum", "electronic")
    lo_fn = lo_scale_fn(cfg, b, drift_path)
    t_mid = t0 + T / 2
    lo_mid = float(lo_fn(t_mid))
    eta_run = eta_of_run(cfg, b, t_mid, drift_path)
    mon_fn = drift_path.monitor_gain if (drift_path is not None and drift_path.stochastic) else None
    lo_on = kind != "electronic"
    spdc_on = kind != "state"
    warnings: list[str] = []

    # ---------------------------------------------------------------- SPDC-Paare und Heralds (wahre Zeit)
    if spdc_on:
        R_pair = src.R_herald / src.eta_herald
        t_pair = _poisson_times(r_src, R_pair, t0, T)
        herald_of_pair = r_src.random(len(t_pair)) < src.eta_herald
        t_dark = _poisson_times(r_src, src.R_dark, t0, T)
    else:
        t_pair = np.empty(0); herald_of_pair = np.empty(0, bool); t_dark = np.empty(0)
    t_h = np.concatenate([t_pair[herald_of_pair], t_dark])
    is_dark = np.concatenate([np.zeros(herald_of_pair.sum(), bool), np.ones(len(t_dark), bool)])
    pair_of_h = np.concatenate([np.flatnonzero(herald_of_pair), -np.ones(len(t_dark), np.int64)])
    o = np.argsort(t_h); t_h, is_dark, pair_of_h = t_h[o], is_dark[o], pair_of_h[o]
    if src.det_dead_s > 0 and len(t_h) > 1:            # Totzeit des Herald-Detektors (SNSPD ~30 ns, APD µs)
        keep = apply_dead_time(t_h, src.det_dead_s)
        if not keep.all():
            lost = np.flatnonzero(~keep & ~is_dark)
            herald_of_pair = herald_of_pair.copy()
            herald_of_pair[pair_of_h[lost]] = False       # verlorene Heralds: Signalphoton bleibt ungeheraldet
            t_h, is_dark, pair_of_h = t_h[keep], is_dark[keep], pair_of_h[keep]
    t_det = t_h + r_src.normal(0, src.sigma_spd, len(t_h))

    # ---------------------------------------------------------------- Moden am Analogeingang
    heralded_state = make_state(src.heralded_state)
    mode_t, mode_x, mode_th, mode_kind, mode_src = [], [], [], [], []
    if spdc_on and not signal_blocked:
        th = phase.theta(t_pair + d.d_S, r_phase)
        x = heralded_state.measure(r_state, th, eta_run)
        mode_t.append(t_pair + d.d_S); mode_x.append(x); mode_th.append(th)
        mode_kind.append(np.where(herald_of_pair, MODE_HERALDED, MODE_UNHERALDED))
        mode_src.append(np.arange(len(t_pair)))
    t_state = np.empty(0)
    test_state = None
    if kind == "state":
        test_state = make_state(b.state)
        t_state = t0 + (np.arange(int(np.floor(T * b.state_rate))) + 0.5) / b.state_rate
        th = phase.theta(t_state + d.d_S, r_phase)
        x = test_state.measure(r_state, th, eta_run)
        mode_t.append(t_state + d.d_S); mode_x.append(x); mode_th.append(th)
        mode_kind.append(np.full(len(t_state), MODE_STATE)); mode_src.append(np.arange(len(t_state)))
    cat = (lambda L, dt: np.concatenate(L) if L else np.empty(0, dt))
    mode_t, mode_x, mode_th = cat(mode_t, float), cat(mode_x, float), cat(mode_th, float)
    mode_kind, mode_src = cat(mode_kind, np.int8), cat(mode_src, np.int64)

    # ---------------------------------------------------------------- Zufallstrigger und Marker (wahre Zeit)
    t_rand = _poisson_times(r_trig, b.random_rate, t0, T) if kind in ("heralded", "random") else np.empty(0)
    k_all = np.arange(int(T * mk.f_marker))
    sent = r_mark.random(len(k_all)) >= mk.p_omit
    t_m = t0 + (0.5 + k_all[sent]) / mk.f_marker
    t_ref = phase.reference_times()

    # ---------------------------------------------------------------- Kopie der ADQ32-Trigger (GPIO → Harp CH2)
    periodic = b.periodic_rate if kind in ("vacuum", "electronic", "free_running") else 0.0
    trig_src_true = []                     # Ereignisse am TRIG-Port (wahre Zeit am Eingang)
    if kind in ("heralded", "herald_blocked"):
        trig_src_true.append(t_det + d.d_T)
    if len(t_rand):
        trig_src_true.append(t_rand + d.d_RT)
    if len(t_state):
        trig_src_true.append(t_state + d.d_RT)
    t_copy = (adq.trigger_copy(r_misc, t0, T, cat(trig_src_true, float), periodic)
              if h.trigger_copy_channel else np.empty(0))
    # Laufmarke (M1) bei Laufbeginn und -ende
    t_run_mark = np.array([t0 + 1e-3, t0 + T - 1e-3]) if h.run_marker_bit else np.empty(0)

    # ---------------------------------------------------------------- Strom 1: HydraHarp 500
    rel = lambda t: t - t0
    inputs = {h.herald_channel: rel(t_det + h.d_H)}
    if len(t_copy):
        inputs[h.trigger_copy_channel] = rel(t_copy + h.d_TC)
    if len(t_rand):
        inputs[h.random_tag_channel] = rel(t_rand + h.d_RH)
    if len(t_state):
        inputs[h.state_tag_channel] = rel(t_state + h.d_RH)
    markers = {}
    if mk.on_timing_channel:
        inputs[h.marker_timing_channel] = rel(t_m + mk.d_MH + r_mark.normal(0, mk.sigma_tc, len(t_m)))
    else:
        markers[h.marker_bit] = rel(t_m + mk.d_MH)
    if pcfg.marker_bit and len(t_ref):
        markers[pcfg.marker_bit] = rel(t_ref)
    if len(t_run_mark):
        markers[h.run_marker_bit] = rel(t_run_mark)
    hr = harp.record(r_harp, T, inputs, markers)
    warnings += hr.warnings
    recs, rec_t = P.encode_t2(hr.times_ps, hr.kinds, hr.codes)

    # ---------------------------------------------------------------- Strom 2: ADQ32
    trig_t, trig_k, trig_i = [], [], []
    if kind in ("heralded", "herald_blocked"):
        trig_t.append(t_det + d.d_T); trig_k.append(np.full(len(t_det), TRIG_HERALD)); trig_i.append(np.arange(len(t_det)))
    if len(t_rand):
        trig_t.append(t_rand + d.d_RT); trig_k.append(np.full(len(t_rand), TRIG_RANDOM)); trig_i.append(np.arange(len(t_rand)))
    if len(t_state):
        trig_t.append(t_state + d.d_RT); trig_k.append(np.full(len(t_state), TRIG_STATE)); trig_i.append(np.arange(len(t_state)))
    trig = TriggerInput(cat(trig_t, float), cat(trig_k, np.int8), cat(trig_i, np.int64))
    xt_amp = d.crosstalk_amp if b.crosstalk_amp is None else b.crosstalk_amp
    dig = adq.acquire(r_dig, t0, T, trig, periodic, mode_t, mode_x, t_m + d.d_MD,
                      t_det + d.d_T, xt_amp, lo_on=lo_on, lo_scale_fn=lo_fn,
                      monitor_gain_fn=mon_fn)
    # LO-Leistungsmonitor (Photodiode), relative Leistung
    if cfg.drift.monitor:
        t_mon = t0 + np.arange(0.0, T, 1.0 / cfg.drift.monitor_rate_Hz)
        p_mon = lo_fn(t_mon) * (1.0 + r_misc.normal(0, cfg.drift.monitor_noise_rel, len(t_mon)))
        if not lo_on:
            p_mon = p_mon * 0.0
        monitor = dict(t_s=t_mon - t0, lo_power_rel=p_mon)
    else:
        monitor = None

    # ---------------------------------------------------------------- Wahrheit und Sollwerte
    rec_of_herald = -np.ones(len(t_h), np.int64)
    rec_of_rand = -np.ones(len(t_rand), np.int64)
    rec_of_state = -np.ones(len(t_state), np.int64)
    for kconst, arr in ((TRIG_HERALD, rec_of_herald), (TRIG_RANDOM, rec_of_rand), (TRIG_STATE, rec_of_state)):
        m = dig.rec_kind == kconst
        arr[dig.rec_index[m]] = np.flatnonzero(m)
    eps = clk.eps
    truth = dict(
        heralds=dict(t_true=t_h, t_det=t_det, is_dark=is_dark, pair=pair_of_h,
                     harp_ps=hr.per_input_ps.get(h.herald_channel, np.empty(0, np.int64)), record=rec_of_herald),
        pairs=dict(t_true=t_pair, heralded=herald_of_pair),
        modes=dict(centre_true=mode_t, centre_D=clk(mode_t) if len(mode_t) else mode_t, x=mode_x, theta=mode_th,
                   kind=mode_kind, source_index=mode_src),
        random=dict(t_true=t_rand, harp_ps=hr.per_input_ps.get(h.random_tag_channel, np.empty(0, np.int64)), record=rec_of_rand),
        state=dict(t_true=t_state, harp_ps=hr.per_input_ps.get(h.state_tag_channel, np.empty(0, np.int64)), record=rec_of_state),
        markers=dict(t_true=t_m, harp_ps=(hr.per_input_ps.get(h.marker_timing_channel) if mk.on_timing_channel
                                          else hr.per_marker_ps.get(h.marker_bit)), recorded_by_digitizer=dig.marker_recorded),
        phase_ref=dict(t_true=t_ref),
        trigger_copy=dict(t_true=t_copy, harp_ps=hr.per_input_ps.get(h.trigger_copy_channel, np.empty(0, np.int64))),
        run_marker=dict(t_true=t_run_mark),
        lo_monitor=dict(lo_rel_true=dig.lo_rel_true if dig.lo_rel_true is not None else np.empty(0)),
        drift=dict(lo_scale_mid=np.array([lo_mid]), eta_run=np.array([eta_run])),
        records=dict(kind=dig.rec_kind, source_index=dig.rec_index, trigger_true=dig.rec_trigger_true,
                     n_modes=dig.n_modes_in_record),
    )
    # Abbildung Harp-Zeit (s, relativ zum Block) → Digitizer-Zeit (s): t_D = (1+ε)(t_H + t0 − t_arm) + φ
    mapping = dict(slope=1 + eps, offset_s=(1 + eps) * (t0 - clk.t_arm), random_walk=bool(clk.D > 0))
    # elektronisches Rauschen ≙ Verlust nach Vakuumnormierung; bei LO-Drift mit der Clearance S·P_LO des Laufs
    # (Sollwerte gelten für eine Normierung mit der Vakuumskala dieses Laufs)
    eta_el = 1.0 / (1.0 + 1.0 / (cfg.clearance * lo_mid))
    expected: dict[str, Any] = dict(eta_electronic=eta_el, mapping=mapping, lo_scale=lo_mid, eta_state=eta_run)
    if kind == "heralded":
        xi = src.R_herald / (src.R_herald + src.R_dark)
        e = heralded_state.expected(eta_run * eta_el)
        expected.update(xi=xi, eta_eff=eta_run * eta_el * xi,
                        W00=xi * e["W00"] + (1 - xi) / np.pi, var_x=xi * e["var_x"] + (1 - xi) * 0.5,
                        note="ohne Beitrag ungeheraldeter/sekundärer Moden und Überlappverluste")
    elif kind in ("vacuum", "herald_blocked"):
        expected.update(W00=1 / np.pi, var_x=0.5)
    elif kind == "electronic":
        expected.update(var_ratio_to_vacuum=(1 / cfg.clearance) / (1 + 1 / cfg.clearance))
    elif kind in ("random", "free_running"):
        R_pair = src.R_herald / src.eta_herald
        g = src.gamma
        n_eff = R_pair * eta_run * eta_el * (2.5 / g if src.mode_shape == "double_exponential" else np.sqrt(np.pi) / g)
        expected.update(W00=(1 / np.pi) * (1 - 2 * n_eff), var_x=0.5 + n_eff, n_eff=n_eff,
                        note="unbedingter Zustand ≈ Vakuum (CW-SPDC, kleine mittlere Photonenzahl je Mode)")
    elif kind == "state":
        e = test_state.expected(eta_run * eta_el, theta=float(pcfg.theta0))
        expected.update(state=test_state.describe(), W00=e["W00"], p_n=e["p_n"], mean_x_at_theta0=e["mean_x"],
                        var_x_at_theta0=e["var_x"], eta_total=eta_run * eta_el, phase_sensitive=test_state.phase_sensitive)
    attrs = dict(dataset_type=KIND_TO_DATASET[kind], kind=kind, sequence_index=index, calibration_id=calibration_id,
                 run_id=run_id, session_id=cfg.session.id, campaign_id=cfg.session.campaign,
                 comparison_role=KIND_TO_ROLE[kind], t_start_s=t0 - cfg.session.start_s,
                 signal_blocked=signal_blocked, lo_on=lo_on, duration_s=T, ptu_file=f"harp_{b.id}.ptu",
                 random_rate=b.random_rate, periodic_rate=periodic, crosstalk_amp=xt_amp,
                 state=json.dumps(test_state.describe()) if test_state else "",
                 harp_channel_roles=json.dumps(cfg.channel_roles(kind)), harp_marker_roles=json.dumps(cfg.marker_roles()),
                 adq32_trigger_source="TRIG" if kind in ("heralded", "herald_blocked", "random", "state") else "PERIODIC",
                 **phase.metadata())
    br = BlockResult(b, index, t0, T, KIND_TO_DATASET[kind], run_id, attrs, recs, rec_t, dig, truth, expected, warnings)
    br.monitor = monitor
    return br


def simulate(cfg: SimConfig) -> RunResult:
    """Simuliert alle Blöcke im Speicher (Grundlage für Datei- und Datenstrom-Modus)."""
    warnings = cfg.validate()
    se = cfg.session
    root = np.random.SeedSequence(se.seed)
    ss_clock, ss_blocks = root.spawn(2)
    t_end = se.start_s + sum(b.duration_s + se.gap_s for b in cfg.blocks) + 1.0
    clk = DigitizerClock(cfg.clock, t_end, np.random.default_rng(ss_clock))
    path = DriftPath(cfg.drift, cfg.lo_monitor, se.start_s, t_end, np.random.default_rng(root.spawn(1)[0]))
    path = path if path.stochastic else None
    adq = ADQ32(cfg, clk)
    harp = HydraHarp500(cfg)
    cal_id = cfg.calibration_id
    blocks = []
    t0 = se.start_s
    for i, (b, ss, rid) in enumerate(zip(cfg.blocks, ss_blocks.spawn(len(cfg.blocks)), cfg.run_ids())):
        br = simulate_block(cfg, b, i, t0, clk, adq, harp, ss, cal_id, rid, drift_path=path)
        br.warnings = warnings + br.warnings
        blocks.append(br)
        t0 += b.duration_s + se.gap_s
    return RunResult(cfg, blocks, adq.device_attrs(), clk, cal_id, se.id, se.campaign, drift=path)


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_run(res: RunResult, out_dir: str | Path) -> dict:
    """Schreibt PTU-Dateien, ``digitizer.h5``, ``truth.h5`` und ``session.json``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cfg = res.cfg
    files: dict[str, str] = {}
    hh = cfg.harp
    for br in res.blocks:
        name = br.attrs["ptu_file"]
        P.write_ptu(out / name, br.harp_records, comment=f"SIMULATED ExSE Harp ADQ32 HHD Mobil – Block {br.cfg.id} ({br.dataset_type})",
                    hw_type=f"{hh.model} (SIMULATED)", acquisition_time_ms=int(round(br.duration * 1000)),
                    extra_tags={"UsrHHD_BlockId": (P.TY_ANSI, br.cfg.id),
                                "UsrHHD_RunId": (P.TY_ANSI, br.run_id),
                                "UsrHHD_RunIndex": (P.TY_ANSI, str(br.index)),
                                "UsrHHD_SessionId": (P.TY_ANSI, res.session_id),
                                "UsrHHD_CampaignId": (P.TY_ANSI, res.campaign_id),
                                "UsrHHD_ComparisonRole": (P.TY_ANSI, br.attrs["comparison_role"]),
                                "UsrHHD_Wavelength_nm": (P.TY_FLOAT8, float(cfg.optics.wavelength_nm)),
                                "UsrHHD_HeraldDetector": (P.TY_ANSI, cfg.optics.herald_detector),
                                "UsrHHD_DatasetType": (P.TY_ANSI, br.dataset_type),
                                "UsrHHD_ChannelRoles": (P.TY_ANSI, br.attrs["harp_channel_roles"]),
                                "UsrHHD_MarkerRoles": (P.TY_ANSI, br.attrs["harp_marker_roles"]),
                                "UsrHHD_CalibrationId": (P.TY_ANSI, res.calibration_id)})
        files[name] = "harp"
    root_attrs = dict(simulated=True, software=f"hhd {__version__}", created=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                      session_id=res.session_id, campaign_id=res.campaign_id, calibration_id=res.calibration_id)
    with S.open_new(out / "digitizer.h5", root_attrs, res.device_attrs) as f:
        for br in res.blocks:
            dg = br.digitizer
            S.write_block(f, br.cfg.id, br.attrs, dg.data, dg.timestamp, dg.record_start, dg.trigger_source, dg.marker_ts,
                          monitor=br.monitor, data_b=dg.data_b)
        cal = f.create_group("calibration")
        cal.attrs["calibration_id"] = res.calibration_id
        cal.attrs["mode"] = "shared"                       # eine Kalibrierung für alle Läufe der Sitzung (ED-12)
        cal.attrs["runs"] = json.dumps([b.run_id for b in res.blocks])
        cal.attrs["vacuum_blocks"] = json.dumps([b.cfg.id for b in res.blocks if b.dataset_type == "vacuum"])
        cal.attrs["electronic_blocks"] = json.dumps([b.cfg.id for b in res.blocks if b.dataset_type == "electronic"])
    files["digitizer.h5"] = "digitizer"
    with h5py.File(out / "truth.h5", "w") as f:
        f.attrs.update(format=S.FORMAT_TRUTH, schema_version=S.SCHEMA_VERSION, software=f"hhd {__version__}",
                       config_json=cfg.to_json())
        ck = f.create_group("clock")
        ck.attrs.update(eps=res.clock.eps, t_arm_s=res.clock.t_arm, rw_D=res.clock.D)
        ck.create_dataset("t_grid", data=res.clock.tg); ck.create_dataset("phi", data=res.clock.phig)
        if res.drift is not None:                         # stochastische Drift: wahrer Verlauf über die Sitzung
            dg = f.create_group("drift")
            for k, v in res.drift.truth().items():
                dg.create_dataset(k, data=v)
        for br in res.blocks:
            g = f.create_group(f"blocks/{br.cfg.id}")
            g.attrs.update(t0_true_s=br.t0, duration_s=br.duration, dataset_type=br.dataset_type, run_id=br.run_id,
                           expected_json=json.dumps(br.expected), warnings_json=json.dumps(br.warnings))
            for grp, dd in br.truth.items():
                gg = g.create_group(grp)
                for k, v in dd.items():
                    gg.create_dataset(k, data=np.asarray(v) if v is not None else np.empty(0))
    files["truth.h5"] = "truth"
    manifest = dict(format="hhd-session", schema_version=S.SCHEMA_VERSION, software=f"hhd {__version__}",
                    simulated=True, session_id=res.session_id, campaign_id=res.campaign_id, seed=cfg.session.seed,
                    optics=dict(wavelength_nm=cfg.optics.wavelength_nm, herald_detector=cfg.optics.herald_detector,
                                homodyne_pd=cfg.optics.homodyne_pd, lo_monitor_pd=cfg.lo_monitor.pd_model),
                    calibration=dict(calibration_id=res.calibration_id, mode="shared",
                                     vacuum_runs=[b.run_id for b in res.blocks if b.dataset_type == "vacuum"]),
                    runs=[dict(run_id=b.run_id, id=b.cfg.id, index=b.index, kind=b.cfg.kind, dataset_type=b.dataset_type,
                               comparison_role=b.attrs["comparison_role"], t_start_s=b.attrs["t_start_s"],
                               duration_s=b.duration, ptu_file=b.attrs["ptu_file"], n_records=int(len(b.digitizer.timestamp)),
                               n_harp_records=int(len(b.harp_records)), expected=b.expected, warnings=b.warnings)
                          for b in res.blocks],
                    files={k: dict(role=v, sha256=_sha256(out / k)) for k, v in files.items()},
                    config=cfg.to_dict())
    (out / MANIFEST).write_text(json.dumps(manifest, indent=1, ensure_ascii=False, default=float), encoding="utf-8")
    res.out_dir = out
    res.files = files
    return manifest


def run(cfg: SimConfig, out_dir: str | Path) -> RunResult:
    res = simulate(cfg)
    write_run(res, out_dir)
    return res


def run_campaign(cfg: SimConfig, out_dir: str | Path, n_sessions: int = 2) -> dict:
    """Kampagne: ``n_sessions`` Sitzungen gleicher Konfiguration mit Seeds seed, seed+1, …

    Jede Sitzung liegt in ``<out>/<session_id>/`` und hat ihre eigene Kalibrierung; ``campaign.json`` listet die
    Sitzungen mit Prüfsumme ihres Manifests (z. B. Seeds-Kampagne für Pull-Tests oder mehrere Messtage).
    """
    import copy
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    sessions = []
    for k in range(n_sessions):
        c = copy.deepcopy(cfg)
        c.session.seed = cfg.session.seed + k
        c.session.id = f"{cfg.session.id}-{k + 1:02d}"
        d = out / c.session.id
        res = run(c, d)
        sessions.append(dict(session_id=c.session.id, dir=c.session.id, seed=c.session.seed,
                             calibration_id=res.calibration_id, runs=[b.run_id for b in res.blocks],
                             manifest_sha256=_sha256(d / MANIFEST)))
    man = dict(format="hhd-campaign", schema_version=S.SCHEMA_VERSION, software=f"hhd {__version__}", simulated=True,
               campaign_id=cfg.session.campaign, sessions=sessions)
    (out / "campaign.json").write_text(json.dumps(man, indent=1, ensure_ascii=False), encoding="utf-8")
    return man
