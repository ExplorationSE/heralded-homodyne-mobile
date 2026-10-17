"""Synchronisation/Zuordnung: Varianten gemeinsame 10 MHz, freie Uhr (ε, Irrfahrt, Marker am M3), fehlende und
zusätzliche Marker, Hochraten-Stresstest, Kampagne, Datenstrom = Batch, keine Wahrheit im Synchronisierer."""
import builtins
import json
import shutil
from pathlib import Path

import h5py
import numpy as np
import pytest

from hhd.io.session import load_session
from hhd.sim.config import BlockCfg, SimConfig
from hhd.sim.engine import run, run_campaign
from hhd.sim.sync_validate import validate_any, validate_session
from hhd.sync import assign as A
from hhd.sync import kernels as K
from hhd.sync.clockmap import fit_clock
from hhd.sync.markers import match_markers
from hhd.sync.pipeline import SyncParams, sync_session
from hhd.sync.stream import stream_run

ROOT = Path(__file__).resolve().parent.parent
FAST = SyncParams(calibrate_delay=False)


@pytest.fixture(scope="module")
def std_sync(std_run):
    _, d = std_run
    rep = sync_session(d, out=d / "sync")
    return d, rep, validate_session(d, rep)


@pytest.fixture(scope="module")
def rw_run(tmp_path_factory):
    """Freie Uhr mit starker Irrfahrt, Marker am Sync-Eingang; zusätzlich ein Zustandslauf (CH4)."""
    d = tmp_path_factory.mktemp("rw")
    cfg = SimConfig()
    cfg.session.seed = 8
    cfg.clock.shared_10MHz = False
    cfg.clock.eps = 2e-6
    cfg.clock.rw_D = 1e-16
    cfg.blocks = [BlockCfg(id="kv", kind="vacuum", duration_s=0.3, periodic_rate=4000),
                  BlockCfg(id="a", kind="heralded", duration_s=2.0),
                  BlockCfg(id="b", kind="free_running", duration_s=0.5, periodic_rate=2000),
                  BlockCfg(id="st", kind="state", duration_s=0.3, state_rate=1000, state={"type": "coherent", "alpha": 1.0})]
    run(cfg, d)
    return d


@pytest.fixture(scope="module")
def stress_run(tmp_path_factory):
    """Hohe Herald-Rate (200 kHz): Totzeit, sekundäre Heralds, überlappende Pretrigger-Fenster."""
    d = tmp_path_factory.mktemp("stress")
    cfg = SimConfig()
    cfg.session.seed = 21
    cfg.source.R_herald = 2e5
    cfg.digitizer.record_length = 1000
    cfg.digitizer.pretrigger = 400
    cfg.blocks = [BlockCfg(id="kv", kind="vacuum", duration_s=0.05, periodic_rate=20000),
                  BlockCfg(id="a", kind="heralded", duration_s=0.15)]
    run(cfg, d)
    return d


# ------------------------------------------------------------------------------------------- Kerne
def test_kernels_grid_xcorr_fit():
    rng = np.random.default_rng(1)
    P = 1e-3
    k = np.flatnonzero(rng.random(3000) > 0.1)
    tH = 0.0005 + k * P + rng.normal(0, 5e-12, len(k))
    keepD = rng.random(len(k)) > 0.01
    kD = k[keepD][37:]                                      # Digitizer startet später, verliert Marker
    TD = np.floor(((kD * P + 0.0005) * (1 + 2e-6) - 0.137 + 23e-9) / 3.2e-9) * 3.2e-9
    gH, gD = K.grid_index(tH, P), K.grid_index(TD, P)
    xc = K.xcorr_shift(gH["k"], gD["k"])
    iH, iD = K.pair_by_index(gH["k"], gD["k"], xc["shift"])
    assert xc["uniqueness"] < 0.5
    assert np.array_equal(k[gH["keep"][iH]], kD[gD["keep"][iD]])
    lin = K.robust_linear(TD[gD["keep"][iD]], tH[gH["keep"][iH]])
    assert abs(lin["b"] * (1 + 2e-6) - 1) < 5 * np.sqrt(lin["cov"][1, 1]) + 1e-12
    # stückweise Fit gibt eine glatte Funktion wieder
    x = np.linspace(0, 1, 2000); r = 1e-9 * np.sin(2 * np.pi * x) + rng.normal(0, 1e-11, len(x))
    c = K.piecewise_fit(x, r, 0.0, 1 / 16, 17)
    assert np.std(K.piecewise_eval(x, c, 0.0, 1 / 16) - 1e-9 * np.sin(2 * np.pi * x)) < 3e-11


def test_kernels_windows():
    ref = np.array([1.0, 2.0, 3.0, 3.05])
    n, f = K.count_in_window(ref, np.array([0.9, 2.95, 5.0]), np.array([1.1, 3.1, 6.0]))
    assert n.tolist() == [1, 2, 0] and f[0] == 0
    w = K.window_index(np.array([0.5, 1.5, 2.5, 9]), np.array([1.0, 2.0]), np.array([1.6, 3.0]))
    assert w.tolist() == [-1, 0, 1, -1]


# ------------------------------------------------------------------------------------------- Standard
def test_shared_clock_standard(std_sync):
    d, rep, v = std_sync
    s = v["summary"]
    assert s["marker_correct"] == s["marker_pairs"] > 0
    assert s["misassigned"] == 0 and s["lost"] == 0
    assert s["mapping_rms_max_s"] < 5e-12
    assert abs(s["pull_a"]["mean"]) < 1.5 and s["pull_a"]["max_abs"] < 5
    assert s["pull_b"]["max_abs"] < 5
    assert rep["quality"]["status"] == "ok", rep["quality"]["warnings"]
    for k, r in rep["runs"].items():
        if r["dataset_type"] == "heralded":
            assert r["refine"]["applied"] and r["refine"]["sigma_after_s"] < 60e-12
            assert r["copy_check"]["fraction_confirmed"] == 1.0
    # eingestreute Zufallstrigger (CH4) und Zufallstrigger-Lauf
    tags = v["runs"]["heralded"]["tags"]["random"]
    assert tags["correct"] == tags["n"] and tags["false_tags"] == 0
    tr = v["runs"]["random"]["tags"]["random"]
    assert tr["correct"] == tr["n"] > 0
    # Ausgabe
    with h5py.File(d / "sync" / "sync.h5") as f:
        g = f["runs/heralded"]
        assert len(g["seg_class"]) == len(g["record_time_H_s"])
        assert json.loads(g.attrs["clockmap"])["b"] == pytest.approx(1.0, abs=1e-9)
    js = json.loads((d / "sync" / "sync.json").read_text(encoding="utf-8"))
    assert js["quality"]["status"] == "ok" and "delta" in js


def test_delta_calibration(std_sync):
    _, rep, v = std_sync
    d = rep["delta"]
    assert d["available"] and d["contrast"] > 0.3
    assert 1e-10 < d["sigma_delta_T_s"] < 5e-9
    assert abs(v["summary"]["delta_T"]["pull"]) < 4
    for k, r in v["runs"].items():
        if "delta_true" in r and "pull" in r["delta_true"]:
            assert abs(r["delta_true"]["pull"]) < 4


def test_free_running_b_time_axis(std_sync):
    _, rep, _ = std_sync
    for k, rs in rep["_syncs"].items():
        if rs.run.dataset_type == "vacuum":
            assert rs.assignment is None
            assert rs.free["segments"]["periodisch"] == rs.run.n_records
            assert rep["runs"][k]["copy_check"]["fraction_confirmed"] > 0.999


# ------------------------------------------------------------------------------------------- freie Uhr
def test_free_clock_marker_input(free_run):
    """ε = 2 ppm, Irrfahrt 1e-17, Sync-Marker am Marker-Eingang M3 (30 ns, Störmarker)."""
    _, d = free_run
    v = validate_session(d)
    s = v["summary"]
    assert s["misassigned"] == 0 and s["lost"] == 0
    assert s["marker_correct"] == s["marker_pairs"]
    assert s["pull_b"]["max_abs"] < 5 and s["pull_a"]["max_abs"] < 5
    h = v["runs"]["heralded"]
    assert h["mapping"]["rms_s"] < 0.5e-9          # nach Verfeinerung über Herald/Trigger-Paare
    assert h["mapping"]["markers_only_rms_s"] > h["mapping"]["rms_s"]
    assert v["quality"]["status"] == "ok", v["quality"]["warnings"]


def test_random_walk_piecewise_and_state(rw_run):
    rep = sync_session(rw_run, write=False)
    v = validate_session(rw_run, rep)
    s = v["summary"]
    assert s["misassigned"] == 0 and s["lost"] == 0
    assert rep["runs"]["a"]["clock"]["model"] == "piecewise"
    assert v["runs"]["a"]["mapping"]["rms_s"] < 1e-9
    assert v["runs"]["b"]["mapping"]["rms_s"] < 2e-9               # Lauf B: Zeitachse nur aus Markern
    st = v["runs"]["st"]["tags"]["state"]
    assert st["correct"] == st["n"] > 0 and st["false_tags"] == 0
    assert abs(s["delta_T"]["pull"]) < 4


# ------------------------------------------------------------------------------------------- Marker manipuliert
def test_missing_and_extra_markers(std_run, tmp_path):
    """Digitizer verliert die ersten 37 und weitere 3 % der SYNC-Marker; Harp erhält 40 Störmarker."""
    _, src = std_run
    ref = load_session(src).run("heralded")
    mH0, mD0 = ref.harp.role_s("sync_marker"), ref.markers_D()
    mm0 = match_markers(mH0, mD0)
    d = tmp_path / "manip"
    shutil.copytree(src, d, ignore=shutil.ignore_patterns("sync*"))
    rng = np.random.default_rng(3)
    with h5py.File(d / "digitizer.h5", "r+") as f:
        g = f["blocks/heralded/markers"]
        ts = g["timestamp"][:]
        keep = np.ones(len(ts), bool); keep[:37] = False; keep[rng.random(len(ts)) < 0.03] = False
        del g["timestamp"]; g["timestamp"] = ts[keep]
    s = load_session(d, verify=False)
    r = s.run("heralded")
    extra = np.sort(rng.uniform(0, r.duration_s, 40))
    mH = np.sort(np.concatenate([mH0, extra]))
    mm = match_markers(mH, r.markers_D())
    assert mm.uniqueness < 0.5 and mm.n_pairs < mm0.n_pairs - 37
    ref_pairs = dict(zip(np.rint(mm0.TD / 1e-12).astype(np.int64).tolist(), mm0.tH.tolist()))
    for td, th in zip(mm.TD, mm.tH):                     # jedes Paar ist auch ein Paar der Originalzuordnung
        assert ref_pairs[int(np.rint(td / 1e-12))] == th
    cm0, _ = fit_clock(mm0.TD, mm0.tH); cm, _ = fit_clock(mm.TD, mm.tH)
    assert abs(cm.a - cm0.a) < 10 * cm0.sigma_r and abs(cm.b - cm0.b) < 1e-9
    rep = sync_session(s, FAST, write=False)
    seg = rep["runs"]["heralded"]["assignment"]["segments"]
    assert seg["zugeordnet"] + seg["zufallstrigger"] == r.n_records
    assert rep["runs"]["heralded"]["marker"]["n_pairs"] < mm0.n_pairs


def test_wrong_marker_shift_detected(std_run):
    """Falscher Versatz um 1 Marker → Herald/Trigger-Zuordnung bricht ein."""
    _, d = std_run
    r = load_session(d).run("heralded")
    mm = match_markers(r.harp.role_s("sync_marker"), r.markers_D())
    iH, iD = K.pair_by_index(K.grid_index(mm.tH, mm.period_H)["k"], K.grid_index(mm.TD, mm.period_D)["k"], 1)
    cm, _ = fit_clock(mm.TD[iD], mm.tH[iH])
    trig = r.trigger_D(); first = r.first_sample_D(); last = first + (r.record_length - 1) / r.fs
    a = A.assign_heralds(np.sort(r.harp.role_s("herald")), cm, trig, first, last, (0, 10), 20e-9, 8e-7, 1.2e-6)
    assert np.mean(a.seg_class == A.SEG_MATCHED) < 0.2


# ------------------------------------------------------------------------------------------- Stress
def test_high_rate_stress(stress_run):
    rep = sync_session(stress_run, FAST, write=False)
    v = validate_session(stress_run, rep)
    s = v["summary"]
    a = rep["runs"]["a"]["assignment"]
    assert s["n_matched"] > 20000
    assert s["misassignment_rate"] < 1e-3
    assert s["efficiency"] > 0.995
    assert a["heralds"]["sekundär"] > 100 and a["reasons"]["Totzeit"] > 100
    assert a["segments_overlapping"] > 100
    assert a["reasons"]["unerklärt"] == 0
    assert v["runs"]["a"]["assignment"]["no_record_but_matched"] == 0
    assert rep["performance"]["events_per_s"] > 1e5


# ------------------------------------------------------------------------------------------- Datenstrom
@pytest.mark.parametrize("fx", ["std_run", "free_run", "stress_run"])
def test_streaming_equals_batch(fx, request):
    d = request.getfixturevalue(fx)
    d = d[1] if isinstance(d, tuple) else d
    rep = sync_session(d, FAST, write=False)
    for k, rs in rep["_syncs"].items():
        if rs.assignment is None:
            continue
        o = stream_run(rs.run, 512)
        assert o["locked"]
        assert np.mean(o["seg_herald"] == rs.assignment.seg_herald) >= 0.999, k
        if rs.run.n_records > 5000:
            assert o["max_buffer"] < 0.8 * (rs.run.n_records + len(rs.run.harp.role_ps("herald")))


# ------------------------------------------------------------------------------------------- Kampagne
def test_campaign_sync_and_pulls(tmp_path):
    cfg = SimConfig()
    cfg.blocks = [BlockCfg(id="kv", kind="vacuum", duration_s=0.2, periodic_rate=5000),
                  BlockCfg(id="kh", kind="herald_blocked", duration_s=0.2),
                  BlockCfg(id="a1", kind="heralded", duration_s=1.0),
                  BlockCfg(id="b", kind="free_running", duration_s=0.3, periodic_rate=2000),
                  BlockCfg(id="a2", kind="heralded", duration_s=1.0)]
    cfg.session.seed = 30
    run_campaign(cfg, tmp_path, n_sessions=3)
    from hhd.sync.pipeline import sync_campaign
    rep = sync_campaign(tmp_path, SyncParams(n_boot=40))
    assert rep["summary"]["n_sessions"] == 3 and rep["summary"]["n_warnings"] == 0
    v = validate_any(tmp_path, SyncParams(n_boot=40))
    assert v["summary"]["misassigned"] == 0
    pulls = [s["summary"]["delta_T"]["pull"] for s in v["sessions"].values()]
    assert all(abs(p) < 4 for p in pulls)
    for s in v["sessions"].values():
        assert s["summary"]["pull_a"]["max_abs"] < 5


# ------------------------------------------------------------------------------------------- Trennung
def test_sync_no_truth(std_run, monkeypatch):
    """Synchronisierer und Importer öffnen truth.h5 nie (Laufzeitprüfung) und erwähnen sie nicht im Code."""
    _, d = std_run
    real_open, real_h5 = builtins.open, h5py.File

    def guard_open(f, *a, **k):
        assert "truth" not in str(f), f
        return real_open(f, *a, **k)

    class GuardH5(real_h5):
        def __init__(self, name, *a, **k):
            assert "truth" not in str(name), name
            super().__init__(name, *a, **k)

    import hhd.io.session as S
    import hhd.sync.pipeline as Pl
    monkeypatch.setattr(builtins, "open", guard_open)
    for mod in (h5py, S.h5py, Pl.h5py):
        monkeypatch.setattr(mod, "File", GuardH5)
    rep = sync_session(d, SyncParams(n_boot=10, delay_max_records=2000), out=d / "sync_guard")
    assert rep["quality"]["n_runs"] == 5
    for p in (ROOT / "hhd" / "sync").glob("*.py"):
        code = p.read_text(encoding="utf-8")
        assert "truth.h5" not in code and "Truth(" not in code and "import sync_validate" not in code, p


def test_cli(std_run, capsys):
    from hhd.cli import main
    _, d = std_run
    assert main(["import", str(d)]) == 0
    assert main(["sync", str(d), "--no-delay", "-o", str(d / "sync_cli")]) == 0
    assert (d / "sync_cli" / "sync.h5").exists()
    assert main(["validate", str(d), "--no-delay"]) == 0
    assert main(["stream", str(d), "--chunk", "512"]) == 0
    out = capsys.readouterr().out
    assert "Fehlzuordnungen" in out and "Übereinstimmung mit Batch" in out
