import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from hhd.io import ptu as P

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import read_ptu_compat  # noqa: E402


def _random_events(rng, n=3000, span_ps=5 * 10 ** 12):
    t = np.sort(rng.integers(0, span_ps, n))
    kinds = rng.choice([P.KIND_EVENT, P.KIND_MARKER, P.KIND_SYNC], n, p=[0.8, 0.15, 0.05])
    codes = np.where(kinds == P.KIND_EVENT, rng.integers(1, 17, n), np.where(kinds == P.KIND_MARKER, rng.integers(1, 16, n), 0))
    return t, kinds, codes


def test_roundtrip_with_overflows(tmp_path):
    rng = np.random.default_rng(1)
    t, k, c = _random_events(rng)
    t[-1] = t[-1] + 3 * (P.T2_WRAP - 1) * P.T2_WRAP          # sehr lange Pause: mehrere Überlauf-Records
    rec, rec_t = P.encode_t2(t, k, c)
    assert np.all(np.diff(rec_t) >= 0)
    P.write_ptu(tmp_path / "a.ptu", rec, comment="Test")
    f = P.read_ptu(tmp_path / "a.ptu")
    assert f.tags["TTResultFormat_TTTRRecType"] == P.REC_GENERIC_T2
    assert f.tags["MeasDesc_GlobalResolution"] == 1e-12
    ev = f.decode()
    m_ev = k != P.KIND_MARKER
    assert np.array_equal(ev.event_time_ps, t[m_ev])
    assert np.array_equal(ev.event_channel, np.where(k[m_ev] == P.KIND_SYNC, 0, c[m_ev]))
    assert np.array_equal(ev.marker_time_ps, t[k == P.KIND_MARKER])
    assert np.array_equal(ev.marker_bits, c[k == P.KIND_MARKER])


def test_chunked_decoding_equals_full():
    rng = np.random.default_rng(2)
    t, k, c = _random_events(rng)
    rec, rec_t = P.encode_t2(t, k, c)
    full = P.T2Decoder().decode(rec)
    dec = P.T2Decoder()
    parts = [dec.decode(ch) for ch in P.iter_chunks(rec, rec_t, np.linspace(0, t.max(), 37)[1:-1].astype(np.int64))]
    assert np.array_equal(np.concatenate([p.event_time_ps for p in parts]), full.event_time_ps)
    assert np.array_equal(np.concatenate([p.marker_time_ps for p in parts]), full.marker_time_ps)


def test_invalid_inputs():
    with pytest.raises(P.PtuError):
        P.encode_t2(np.array([1]), np.array([P.KIND_EVENT]), np.array([0]))
    with pytest.raises(P.PtuError):
        P.encode_t2(np.array([1]), np.array([P.KIND_MARKER]), np.array([16]))
    with pytest.raises(P.PtuError):
        P.encode_t2(np.array([-1]), np.array([P.KIND_EVENT]), np.array([1]))


def test_read_ptu_compatible_reader(std_run):
    """Roundtrip Simulator → PTU → Read_PTU-kompatibler Leser (unabhängige, bitweise Dekodierung)."""
    res, d = std_run
    br = [b for b in res.blocks if b.cfg.id == "heralded"][0]
    events, glob = read_ptu_compat.read_records(str(d / br.attrs["ptu_file"]))
    assert glob == 1e-12
    chn = np.array([(a, b) for _, typ, a, b in events if typ == "CHN"])
    ev = P.read_ptu(d / br.attrs["ptu_file"]).decode()
    assert np.array_equal(chn[:, 1], ev.event_time_ps)
    assert np.array_equal(chn[:, 0], ev.event_channel)
    # Herald-Zeiten in der Datei = Wahrheit (ps-genau)
    hps = br.truth["heralds"]["harp_ps"]
    assert np.array_equal(np.sort(hps[hps >= 0]), ev.channel(res.cfg.harp.herald_channel))


@pytest.mark.skipif(not os.environ.get("HHD_READ_PTU_PY"), reason="HHD_READ_PTU_PY (Pfad zu PicoQuants Read_PTU.py) nicht gesetzt")
def test_read_ptu_official(std_run, tmp_path):
    """Zeilengenauer Vergleich mit dem offiziellen PicoQuant-Demo-Leser (nicht Teil des Repositorys)."""
    res, d = std_run
    ptu = d / "harp_heralded.ptu"
    out_off = tmp_path / "official.txt"
    subprocess.run([sys.executable, os.environ["HHD_READ_PTU_PY"], str(ptu), str(out_off)], check=True,
                   capture_output=True)
    ev, glob = read_ptu_compat.read_records(str(ptu))
    out_own = tmp_path / "own.txt"
    read_ptu_compat.write_text(ev, glob, str(out_own))
    off_lines = out_off.read_text(encoding="utf-8").split("-----------------------\n", 1)[1].splitlines()
    off_lines = [ln for ln in off_lines if ln and ln[0].isdigit()]
    assert "PQ Generic T2 data" in out_off.read_text(encoding="utf-8")
    assert off_lines == out_own.read_text(encoding="utf-8").splitlines()
