"""Kurze Laufpläne für weitere geheraldete Zustände (Server: Laufplan „kurz_<zustand>“).

Aufbau wie „kurz“ (K-V, Dunkel, K-H, A, B, A, K-V), aber mit längeren Läufen (A 2 × 3 s, K-V 2 × 1 s), damit
W(0,0) und p_n auch für Mehrphotonenzustände aussagekräftig sind. Der Zustand der geheraldeten Mode kommt aus
``hhd.sim.states.STATE_PRESETS``. Bei phasenempfindlichen Zuständen (Katze, Kätzchen, kohärent, gequetscht,
Überlagerung) wird die LO-Phase zufällig gesetzt: Die Auswertung v1.0 ist phasengemittelt, die Sollwerte ebenso.

Ab 0.15 gibt es für jeden phasenempfindlichen Zustand zusätzlich „kurzphase_<zustand>“ (Gruppe „Mit Phase
(Piezo-Rampe)“): LO-Phase aus einer Piezo-Rampe im LO-Arm (``phase.mode = piezo``), Rampenspannung je Record
(``records/ramp_raw``), drei kohärente Referenzläufe K-θ für die Kalibrierung Spannung → θ und phasenaufgelöste
MaxLik-Tomografie (``hhd.analysis.phase``). Ablauf: K-V, Dunkel, K-θ, K-H, A (10 s), K-θ, B, A (10 s), K-θ, K-V.
"""
from __future__ import annotations

from .states import STATE_PRESETS, make_state

# Planname → Voreinstellung (Reihenfolge = Anzeige)
STATE_PLAN_KEYS = ["fock2", "fock3", "fock01", "cat_even_1", "cat_odd_1", "cat_even_2", "cat_odd_2", "kitten_3dB",
                   "coherent_1", "squeezed_3dB", "squeezed_6dB", "thermal_0.5"]
STATE_PLANS = {f"kurz_{k}": k for k in STATE_PLAN_KEYS}
DURATIONS = {"vacuum": 1.0, "electronic": 0.3, "herald_blocked": 0.7, "heralded": 3.0, "free_running": 1.0,
             "random": 1.0}


PHASE_PLAN_KEYS = [k for k in STATE_PLAN_KEYS if make_state(k).phase_sensitive]
PHASE_PLANS = {f"kurzphase_{k}": k for k in PHASE_PLAN_KEYS}
PHASE_DURATIONS = dict(DURATIONS, heralded=10.0, phase_ref=1.0)


def phase_plan_label(key: str) -> str:
    return f"Kurz mit Phase: {STATE_PRESETS[key]['label']} (10 Läufe)"


def phase_plan_doc(key: str) -> str:
    return (f"K-V, Dunkel, K-θ, K-H, A (10 s), K-θ, B, A (10 s), K-θ, K-V; geheraldeter Zustand "
            f"{STATE_PRESETS[key]['label']} · LO-Phase aus Piezo-Rampe (Dreieck), kalibriert mit K-θ → "
            f"phasenaufgelöste MaxLik-Tomografie, 2D-Wigner, Var(x_θ)")


def apply_phase_plan(cfg, plan: str):
    """Konfiguration für ``kurzphase_<zustand>``: wie „kurz_<zustand>“, aber Piezo-Rampe, K-θ-Läufe, A je 10 s."""
    from ..server.devices import short_plan
    from .config import BlockCfg
    key = PHASE_PLANS[plan]
    blocks = short_plan(cfg.blocks)
    for b in blocks:
        if b.kind in PHASE_DURATIONS:
            b.duration_s = PHASE_DURATIONS[b.kind]
        if isinstance(b.phase, dict) and "mode" in b.phase:
            b.phase = {k: v for k, v in b.phase.items() if k != "mode"}
    kt = lambda i: BlockCfg(id=f"kt_{i:02d}", kind="phase_ref", duration_s=PHASE_DURATIONS["phase_ref"])
    out, n_kt, n_a = [], 0, 0
    for b in blocks:
        if b.kind == "herald_blocked" and n_kt == 0:          # K-θ vor K-H (nach Dunkel)
            out.append(kt(n_kt)); n_kt += 1
        out.append(b)
        if b.kind == "heralded":
            n_a += 1
            out.append(kt(n_kt)); n_kt += 1                    # K-θ nach jedem A
    if n_kt == 0:
        out.insert(0, kt(0))
    cfg.blocks = out
    cfg.source.heralded_state = key
    cfg.phase.mode = "piezo"
    if cfg.phase.ramp_channel == "none":
        cfg.phase.ramp_channel = "aux_adc"
    return cfg


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
