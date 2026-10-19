"""Kurze Laufpläne für weitere geheraldete Zustände (Server: Laufplan „kurz_<zustand>“).

Aufbau wie „kurz“ (K-V, Dunkel, K-H, A, B, A, K-V), aber mit längeren Läufen (A 2 × 3 s, K-V 2 × 1 s), damit
W(0,0) und p_n auch für Mehrphotonenzustände aussagekräftig sind. Der Zustand der geheraldeten Mode kommt aus
``hhd.sim.states.STATE_PRESETS``. Bei phasenempfindlichen Zuständen (Katze, Kätzchen, kohärent, gequetscht,
Überlagerung) wird die LO-Phase zufällig gesetzt: Die Auswertung v1.0 ist phasengemittelt, die Sollwerte ebenso.
"""
from __future__ import annotations

from .states import STATE_PRESETS, make_state

# Planname → Voreinstellung (Reihenfolge = Anzeige)
STATE_PLAN_KEYS = ["fock2", "fock3", "fock01", "cat_even_1", "cat_odd_1", "cat_even_2", "cat_odd_2", "kitten_3dB",
                   "coherent_1", "squeezed_3dB", "squeezed_6dB", "thermal_0.5"]
STATE_PLANS = {f"kurz_{k}": k for k in STATE_PLAN_KEYS}
DURATIONS = {"vacuum": 1.0, "electronic": 0.3, "herald_blocked": 0.7, "heralded": 3.0, "free_running": 1.0,
             "random": 1.0}


def plan_label(key: str) -> str:
    return f"Kurz: {STATE_PRESETS[key]['label']} (7 Läufe)"


def plan_doc(key: str) -> str:
    st = make_state(key)
    extra = " · phasenempfindlich, Phase zufällig → phasengemittelte Auswertung" if st.phase_sensitive else ""
    multi = " · Herald mit Photonenzahlauflösung angenommen" if key in ("fock2", "fock3") else ""
    return f"K-V, Dunkel, K-H, A (3 s), B, A (3 s), K-V; geheraldeter Zustand {STATE_PRESETS[key]['label']}{extra}{multi}"


def apply_state_plan(cfg, plan: str):
    """Konfiguration für den Laufplan ``kurz_<zustand>`` anpassen (Blöcke wie „kurz“, Dauern, Zustand, Phase)."""
    from ..server.devices import short_plan
    key = STATE_PLANS[plan]
    cfg.blocks = short_plan(cfg.blocks)
    for b in cfg.blocks:
        if b.kind in DURATIONS:
            b.duration_s = DURATIONS[b.kind]
    cfg.source.heralded_state = key
    if make_state(key).phase_sensitive:
        cfg.phase.mode = "random"
        for b in cfg.blocks:
            if isinstance(b.phase, dict) and "mode" in b.phase:
                b.phase = {k: v for k, v in b.phase.items() if k != "mode"}
    return cfg
