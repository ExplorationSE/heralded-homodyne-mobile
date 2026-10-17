from pathlib import Path

from hhd.sim.cli import main

ROOT = Path(__file__).resolve().parent.parent


def test_cli_end_to_end(tmp_path, capsys):
    assert main(["params"]) == 0
    assert main(["config", "-o", str(tmp_path / "c.yaml")]) == 0
    out = tmp_path / "lauf"
    assert main(["run", str(ROOT / "config" / "standard.yaml"), "-o", str(out), "--scale-duration", "0.04", "--seed", "4"]) == 0
    assert main(["validate", str(out)]) == 0
    assert main(["check", str(out), "--boot", "5"]) == 0
    assert main(["check", str(out), "--boot", "5", "--norm", "interpolated"]) == 0
    assert main(["campaign", "--scale-duration", "0.02", "-n", "2", "-o", str(tmp_path / "kamp")]) == 0
    assert (tmp_path / "kamp" / "campaign.json").exists()
    assert main(["stream", "--scale-duration", "0.02", "--no-realtime", "--max-chunks", "3"]) == 0
    txt = capsys.readouterr().out
    assert "Vakuumnormierung" in txt and "heralded" in txt and "A − B" in txt and "Kampagne" in txt
    # Manipulation wird erkannt
    p = out / "harp_a_01.ptu"
    b = bytearray(p.read_bytes()); b[-1] ^= 0xFF; p.write_bytes(bytes(b))
    assert main(["validate", str(out)]) == 1


def test_cli_config_error(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("harp: {n_channels: 40}\n", encoding="utf-8")
    assert main(["run", str(bad), "-o", str(tmp_path / "x")]) == 2
    assert "Konfigurationsfehler" in capsys.readouterr().err
