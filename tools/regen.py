"""Erzeugt config/standard.yaml und docs/PARAMETER.md aus hhd/sim/config.py neu (Tests prüfen den Gleichstand)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from hhd.sim.config import SimConfig, parameter_markdown  # noqa: E402

head = ("# Standardkonfiguration des Simulators (alle Parameter, Defaults aus hhd/sim/config.py).\n"
        "# Messablauf ED-12: getrennte Läufe, Default = M1-Laufplan (config/laufplan_m1.yaml) im Zeitraffer 1/90.\n"
        "# Optik (Entscheidung 17.10.2026): 1550 nm, Herald-Detektor SNSPD (Preset), Homodyn InGaAs, LO-Monitor PDA05CF2 + 10 dB.\n"
        "# Geräte: HydraHarp 500 S (Sync + 4 Kanäle), ADQ32-PCIe S2G5 (2,5 GS/s), BW1G0 (1 GHz); LO-Monitor an Kanal B.\n"
        "# Bedeutung, Einheit, Status und Quelle jedes Parameters: docs/PARAMETER.md\n")
(ROOT / "config" / "standard.yaml").write_text(head + SimConfig().to_yaml(), encoding="utf-8")
(ROOT / "docs" / "PARAMETER.md").write_text(parameter_markdown(), encoding="utf-8")
print("config/standard.yaml, docs/PARAMETER.md aktualisiert")
