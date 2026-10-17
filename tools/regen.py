"""Erzeugt config/standard.yaml und docs/PARAMETER.md aus hhd/sim/config.py neu (Tests prüfen den Gleichstand)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from hhd.sim.config import SimConfig, parameter_markdown  # noqa: E402

head = ("# Standardkonfiguration des Simulators (alle Parameter, Defaults aus hhd/sim/config.py).\n"
        "# Messablauf ED-12: getrennte Läufe K-V → K-H → A (Herald) → B (ohne Herald) → A → K-V, gemeinsame Kalibrierung.\n"
        "# Geräte: HydraHarp 500 S (4 Kanäle), ADQ32-PCIe S2G5 (2,5 GS/s), BW1G0 (1 GHz).\n"
        "# Bedeutung, Einheit, Status und Quelle jedes Parameters: docs/PARAMETER.md\n")
(ROOT / "config" / "standard.yaml").write_text(head + SimConfig().to_yaml(), encoding="utf-8")
(ROOT / "docs" / "PARAMETER.md").write_text(parameter_markdown(), encoding="utf-8")
print("config/standard.yaml, docs/PARAMETER.md aktualisiert")
