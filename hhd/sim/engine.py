"""Simulation eines Messlaufs aus mehreren Blöcken (Messablauf laut Gesamtplan AP18).

Ein Lauf erzeugt je Block eine PTU-Datei der HydraHarp 500 (Strom 1), eine gemeinsame Digitizer-Datei im
HDF5-Schema v1 (Strom 2), eine getrennte Wahrheitsdatei und ein Manifest ``run.json``.

Blockarten (``BlockCfg.kind``) und Physik:

=================  ================  ======================  ===========================================
kind               dataset_type      Trigger am ADQ32        Signalmoden
=================  ================  ======================  ===========================================
heralded           heralded          Herald (+ Zufall, OR)   alle SPDC-Paare (geheraldet + ungeheraldet)
random             random_trigger    Zufall                  alle SPDC-Paare (unbedingter Zustand)
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
from .digitizer import ADQ32, TRIG_HERALD, TRIG_RANDOM, TRIG_STATE, TriggerInput
from .harp import HydraHarp500
from .phase import PhaseModel
from .states import make_state

KIND_TO_DATASET = dict(heralded="heralded", random="random_trigger", herald_blocked="herald_blocked",
                       vacuum="vacuum", electronic="electronic", state="prepared_state")
MODE_HERALDED, MODE_UNHERALDED, MODE_STATE = 0, 1, 2


@dataclass
class BlockResult:
    cfg: BlockCfg
    index: int
    t0: float
    duration: float
    dataset_type: str
    attrs: dict
    harp_records: np.ndarray            # T2-Records (uint32)
    harp_record_time_ps: np.ndarray
    digitizer: Any                      # DigitizerRecording
    truth: dict
    expected: dict
    warnings: list = field(default_factory=list)


@dataclass
class RunResult:
    cfg: SimConfig
    blocks: list[BlockResult]
    device_attrs: dict
    clock: DigitizerClock
    calibration_id: str
    out_dir: Path | None = None
    files: dict = field(default_factory=dict)


def _poisson_times(rng, rate, t0, T):
    n = rng.poisson(rate * T) if rate > 0 else 0
    return np.sort(rng.uniform(t0, t0 + T, n))


def simulate_block(cfg: SimConfig, b: BlockCfg, index: int, t0: float, clk: DigitizerClock, adq: ADQ32,
                   harp: HydraHarp500, ss: np.random.SeedSequence, calibration_id: str) -> BlockResult:
    rs = [np.random.default_rng(s) for s in ss.spawn(8)]
    r_src, r_state, r_phase, r_harp, r_dig, r_mark, r_trig, r_misc = rs
    src, h, d, mk = cfg.source, cfg.harp, cfg.digitizer, cfg.markers
    T = b.duration_s
    kind = b.kind
    pcfg = cfg.phase_for(b)
    phase = PhaseModel(pcfg, t0, T)
    signal_blocked = kind in ("herald_blocked", "vacuum", "electronic")
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
    t_det = t_h + r_src.normal(0, src.sigma_spd, len(t_h))

    # ---------------------------------------------------------------- Moden am Analogeingang
    heralded_state = make_state(src.heralded_state)
    mode_t, mode_x, mode_th, mode_kind, mode_src = [], [], [], [], []
    if spdc_on and not signal_blocked:
        th = phase.theta(t_pair + d.d_S, r_phase)
        x = heralded_state.measure(r_state, th, src.eta)
        mode_t.append(t_pair + d.d_S); mode_x.append(x); mode_th.append(th)
        mode_kind.append(np.where(herald_of_pair, MODE_HERALDED, MODE_UNHERALDED))
        mode_src.append(np.arange(len(t_pair)))
    t_state = np.empty(0)
    test_state = None
    if kind == "state":
        test_state = make_state(b.state)
        t_state = t0 + (np.arange(int(np.floor(T * b.state_rate))) + 0.5) / b.state_rate
        th = phase.theta(t_state + d.d_S, r_phase)
        x = test_state.measure(r_state, th, src.eta)
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

    # ---------------------------------------------------------------- Strom 1: HydraHarp 500
    rel = lambda t: t - t0
    inputs = {h.herald_channel: rel(t_det + h.d_H)}
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
    periodic = b.periodic_rate if kind in ("vacuum", "electronic") else 0.0
    xt_amp = d.crosstalk_amp if b.crosstalk_amp is None else b.crosstalk_amp
    dig = adq.acquire(r_dig, t0, T, trig, periodic, mode_t, mode_x, t_m + d.d_MD,
                      t_det + d.d_T, xt_amp, lo_on=lo_on)

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
        records=dict(kind=dig.rec_kind, source_index=dig.rec_index, trigger_true=dig.rec_trigger_true,
                     n_modes=dig.n_modes_in_record),
    )
    # Abbildung Harp-Zeit (s, relativ zum Block) → Digitizer-Zeit (s): t_D = (1+ε)(t_H + t0 − t_arm) + φ
    mapping = dict(slope=1 + eps, offset_s=(1 + eps) * (t0 - clk.t_arm), random_walk=bool(clk.D > 0))
    eta_el = 1.0 / (1.0 + 1.0 / cfg.clearance)     # elektronisches Rauschen ≙ Verlust nach Vakuumnormierung
    expected: dict[str, Any] = dict(eta_electronic=eta_el, mapping=mapping)
    if kind == "heralded":
        xi = src.R_herald / (src.R_herald + src.R_dark)
        e = heralded_state.expected(src.eta * eta_el)
        expected.update(xi=xi, eta_eff=src.eta * eta_el * xi,
                        W00=xi * e["W00"] + (1 - xi) / np.pi, var_x=xi * e["var_x"] + (1 - xi) * 0.5,
                        note="ohne Beitrag ungeheraldeter/sekundärer Moden und Überlappverluste")
    elif kind in ("vacuum", "herald_blocked"):
        expected.update(W00=1 / np.pi, var_x=0.5)
    elif kind == "electronic":
        expected.update(var_ratio_to_vacuum=(1 / cfg.clearance) / (1 + 1 / cfg.clearance))
    elif kind == "random":
        R_pair = src.R_herald / src.eta_herald
        g = src.gamma
        n_eff = R_pair * src.eta * eta_el * (2.5 / g if src.mode_shape == "double_exponential" else np.sqrt(np.pi) / g)
        expected.update(W00=(1 / np.pi) * (1 - 2 * n_eff), var_x=0.5 + n_eff, n_eff=n_eff,
                        note="unbedingter Zustand ≈ Vakuum (CW-SPDC, kleine mittlere Photonenzahl je Mode)")
    elif kind == "state":
        e = test_state.expected(src.eta * eta_el, theta=float(pcfg.theta0))
        expected.update(state=test_state.describe(), W00=e["W00"], p_n=e["p_n"], mean_x_at_theta0=e["mean_x"],
                        var_x_at_theta0=e["var_x"], eta_total=src.eta * eta_el, phase_sensitive=test_state.phase_sensitive)
    attrs = dict(dataset_type=KIND_TO_DATASET[kind], kind=kind, sequence_index=index, calibration_id=calibration_id,
                 signal_blocked=signal_blocked, lo_on=lo_on, duration_s=T, ptu_file=f"harp_{b.id}.ptu",
                 random_rate=b.random_rate, periodic_rate=periodic, crosstalk_amp=xt_amp,
                 state=json.dumps(test_state.describe()) if test_state else "", **phase.metadata())
    return BlockResult(b, index, t0, T, KIND_TO_DATASET[kind], attrs, recs, rec_t, dig, truth, expected, warnings)


def simulate(cfg: SimConfig) -> RunResult:
    """Simuliert alle Blöcke im Speicher (Grundlage für Datei- und Datenstrom-Modus)."""
    warnings = cfg.validate()
    root = np.random.SeedSequence(cfg.run.seed)
    ss_clock, ss_blocks = root.spawn(2)
    t_end = cfg.run.start_s + sum(b.duration_s + cfg.run.gap_s for b in cfg.blocks) + 1.0
    clk = DigitizerClock(cfg.clock, t_end, np.random.default_rng(ss_clock))
    adq = ADQ32(cfg, clk)
    harp = HydraHarp500(cfg)
    cal_id = f"{cfg.run.name}-cal-{cfg.run.seed}"
    blocks = []
    t0 = cfg.run.start_s
    for i, (b, ss) in enumerate(zip(cfg.blocks, ss_blocks.spawn(len(cfg.blocks)))):
        br = simulate_block(cfg, b, i, t0, clk, adq, harp, ss, cal_id)
        br.warnings = warnings + br.warnings
        blocks.append(br)
        t0 += b.duration_s + cfg.run.gap_s
    return RunResult(cfg, blocks, adq.device_attrs(), clk, cal_id)


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_run(res: RunResult, out_dir: str | Path) -> dict:
    """Schreibt PTU-Dateien, ``digitizer.h5``, ``truth.h5`` und ``run.json``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cfg = res.cfg
    files: dict[str, str] = {}
    hh = cfg.harp
    chan_roles = {"herald": hh.herald_channel, "random_tag": hh.random_tag_channel, "state_tag": hh.state_tag_channel}
    if cfg.markers.on_timing_channel:
        chan_roles["sync_marker"] = hh.marker_timing_channel
    for br in res.blocks:
        name = br.attrs["ptu_file"]
        P.write_ptu(out / name, br.harp_records, comment=f"SIMULATED ExSE Harp ADQ32 HHD Mobil – Block {br.cfg.id} ({br.dataset_type})",
                    hw_type=f"{hh.model} (SIMULATED)", acquisition_time_ms=int(round(br.duration * 1000)),
                    extra_tags={"UsrHHD_BlockId": (P.TY_ANSI, br.cfg.id),
                                "UsrHHD_DatasetType": (P.TY_ANSI, br.dataset_type),
                                "UsrHHD_ChannelRoles": (P.TY_ANSI, json.dumps(chan_roles)),
                                "UsrHHD_MarkerBits": (P.TY_ANSI, json.dumps({"sync": 0 if cfg.markers.on_timing_channel else hh.marker_bit,
                                                                             "phase_ref": int(br.attrs["phase_marker_bit"])})),
                                "UsrHHD_CalibrationId": (P.TY_ANSI, res.calibration_id)})
        files[name] = "harp"
    root_attrs = dict(simulated=True, software=f"hhd {__version__}", created=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                      run_name=cfg.run.name)
    with S.open_new(out / "digitizer.h5", root_attrs, res.device_attrs) as f:
        for br in res.blocks:
            dg = br.digitizer
            S.write_block(f, br.cfg.id, br.attrs, dg.data, dg.timestamp, dg.record_start, dg.trigger_source, dg.marker_ts)
        cal = f.create_group("calibration")
        cal.attrs["calibration_id"] = res.calibration_id
        cal.attrs["vacuum_blocks"] = json.dumps([b.cfg.id for b in res.blocks if b.dataset_type == "vacuum"])
        cal.attrs["electronic_blocks"] = json.dumps([b.cfg.id for b in res.blocks if b.dataset_type == "electronic"])
    files["digitizer.h5"] = "digitizer"
    with h5py.File(out / "truth.h5", "w") as f:
        f.attrs.update(format=S.FORMAT_TRUTH, schema_version=S.SCHEMA_VERSION, software=f"hhd {__version__}",
                       config_json=cfg.to_json())
        ck = f.create_group("clock")
        ck.attrs.update(eps=res.clock.eps, t_arm_s=res.clock.t_arm, rw_D=res.clock.D)
        ck.create_dataset("t_grid", data=res.clock.tg); ck.create_dataset("phi", data=res.clock.phig)
        for br in res.blocks:
            g = f.create_group(f"blocks/{br.cfg.id}")
            g.attrs.update(t0_true_s=br.t0, duration_s=br.duration, dataset_type=br.dataset_type,
                           expected_json=json.dumps(br.expected), warnings_json=json.dumps(br.warnings))
            for grp, dd in br.truth.items():
                gg = g.create_group(grp)
                for k, v in dd.items():
                    gg.create_dataset(k, data=np.asarray(v) if v is not None else np.empty(0))
    files["truth.h5"] = "truth"
    manifest = dict(format="hhd-run", schema_version=S.SCHEMA_VERSION, software=f"hhd {__version__}",
                    simulated=True, run_name=cfg.run.name, seed=cfg.run.seed, calibration_id=res.calibration_id,
                    blocks=[dict(id=b.cfg.id, kind=b.cfg.kind, dataset_type=b.dataset_type, duration_s=b.duration,
                                 ptu_file=b.attrs["ptu_file"], n_records=int(len(b.digitizer.timestamp)),
                                 n_harp_records=int(len(b.harp_records)), expected=b.expected, warnings=b.warnings)
                            for b in res.blocks],
                    files={k: dict(role=v, sha256=_sha256(out / k)) for k, v in files.items()},
                    config=cfg.to_dict())
    (out / "run.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False, default=float), encoding="utf-8")
    res.out_dir = out
    res.files = files
    return manifest


def run(cfg: SimConfig, out_dir: str | Path) -> RunResult:
    res = simulate(cfg)
    write_run(res, out_dir)
    return res
