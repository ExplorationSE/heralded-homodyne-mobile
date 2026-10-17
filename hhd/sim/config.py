"""Konfiguration des Simulators (YAML/JSON) mit Parametertabelle.

Jeder Parameter trägt Einheit, Status und Quelle. Status:

* ``Spez.``     – aus Herstellerunterlagen (HydraHarp-500-Handbuch, ADQ32-Datenblatt 20-2378, ADQ3 User Guide 21-2539);
* ``Annahme``   – plausibler Wert ohne Herstellerbezug;
* ``Vorschlag`` – Entwurfsentscheidung (änderbar);
* ``Vorbericht``– aus den Vorberichten (Synchronisationsbericht, Kap. „Parameter (Defaults)“).

Die Tabelle ``docs/PARAMETER.md`` wird mit ``hhd-sim params --markdown`` aus diesem Modul erzeugt.
"""
from __future__ import annotations

import copy
import json
from dataclasses import MISSING, asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

HH500_MANUAL = "HydraHarp 500 Manual, Kap. 9.3.1"
HH500_T2 = "HydraHarp 500 Manual, Kap. 6.3 (T2)"
ADQ32_DS = "ADQ32 Datenblatt 20-2378"
ADQ3_UG = "ADQ3 User Guide 21-2539"
SYNC_REP = "Vorbericht Synchronisation, Parameter"
DEC_16 = "Entscheidung Herr Eilers (Gesamtplan v1.6)"
SQ_EOS = "Single Quantum Eos, Broschüre 2026/03"
IDQ_281 = "ID Quantique ID281 (Produktseite)"
IDQ_QUBE = "ID Quantique ID Qube NIR Free-Running (Broschüre)"
PDA05 = "Thorlabs PDA05CF2, Bedienungsanleitung TTN134810-D02"

# Herald-Detektor-Presets (Gesamtplan v1.6). Typische Herstellerwerte bei 1550 nm, konservativ gewählt:
# SNSPD: Systemeffizienz ≥ 80–90 % (IDQ ID281), ≥ 90 % (Single Quantum Eos); Jitter < 40 ps FWHM (ID281 Standard),
# ≤ 15 ps (Eos); Dunkelrate < 100 cps (ID281 Standard 1300–1600 nm), ≤ 1 cps (Eos); Erholzeit typ. < 30 ns
# (ID281, 50 %-Erholung). InGaAs-APD frei laufend (ID Qube NIR FR): 10/15/20(/25) % Effizienz, Jitter typ. 150 ps
# FWHM (max. 200 ps), Dunkelrate max. 3 kHz (LN) bzw. 6 kHz (STD) bei 20 %, Totzeit 0,1–80 µs einstellbar.
FWHM = 2.0 * (2.0 * 0.6931471805599453) ** 0.5
HERALD_PRESETS = {
    "snspd": dict(det_eff=0.85, R_dark=100.0, sigma_spd=40e-12 / FWHM, det_dead_s=30e-9),
    "ingaas_apd": dict(det_eff=0.20, R_dark=3000.0, sigma_spd=150e-12 / FWHM, det_dead_s=10e-6),
}
HERALD_ARM_T = 0.6          # Transmission Herald-Arm (Filter, Kopplung) [Annahme]


def lo_monitor_levels(responsivity_A_W: float, gain_V_A: float, attenuator_dB: float, p_tap_W: float,
                      dark_V: float, noise_V: float, dc_offset_V: float, adc_noise_lsb: float = 3.0,
                      vpp: float = 0.5, bits: int = 12) -> dict:
    """Pegel der LO-Monitor-Photodiode an ADQ32-Kanal B (Kette Photodiode → Dämpfer → 50-Ω-Eingang).

    Code = (U + DC-Offset)/LSB mit LSB = Vpp/2^bits; Rückgabe in LSB und Volt."""
    a = 10 ** (-attenuator_dB / 20)
    lsb = vpp / 2 ** bits
    v_sig = p_tap_W * responsivity_A_W * gain_V_A * a
    return dict(lsb_V=lsb, v_signal=v_sig, v_dark=dark_V * a, v_max_at_adc=5.0 * a,
                lsb_nominal=round(v_sig / lsb, 1), dark_lsb=round((dark_V * a + dc_offset_V) / lsb, 1),
                noise_lsb=round(float(((noise_V * a / lsb) ** 2 + adc_noise_lsb ** 2) ** 0.5), 2))


_PD = dict(responsivity_A_W=1.0, gain_V_A=5000.0, attenuator_dB=10.0, p_tap_W=150e-6, dark_V=0.005, noise_V=2e-3,
           dc_offset_V=-0.2)
_PD_LEVELS = lo_monitor_levels(**_PD)


def P(default, unit="", status="Annahme", source="", doc=""):
    """Feld mit Metadaten für die Parametertabelle."""
    md = dict(unit=unit, status=status, source=source, doc=doc)
    if isinstance(default, (list, dict)):
        return field(default_factory=lambda d=default: copy.deepcopy(d), metadata=md)
    return field(default=default, metadata=md)


class ConfigError(ValueError):
    pass


# ============================================================================ Gruppen
@dataclass
class SessionCfg:
    id: str = P("S001", doc="Sitzungs-ID: eine Sitzung = Folge von Messläufen mit gemeinsamer Kalibrierung", status="Vorschlag", source="ED-12")
    campaign: str = P("K-SIM", doc="Kampagnen-ID (mehrere Sitzungen, z. B. Seeds-Kampagne oder Messtage)", status="Vorschlag")
    seed: int = P(20261020, doc="Startwert des Zufallsgenerators; gleicher Seed = bitgleiche Dateien", status="Vorschlag")
    start_s: float = P(0.25, "s", doc="wahre Startzeit des ersten Laufs (Digitizer-Zähler läuft ab clock.t_arm_s)", status="Vorschlag")
    gap_s: float = P(0.5, "s", doc="Pause zwischen Läufen (Umbau, z. B. Blocker, Triggerquelle umschalten)", status="Vorschlag")


@dataclass
class DriftCfg:
    lo_rel_per_s: float = P(0.0, "1/s", doc="lineare relative Drift der LO-Leistung ab Sitzungsbeginn (0 = aus); Vakuumrauschen ∝ √P_LO, elektronisches Rauschen fest", status="Vorschlag", source="Gesamtplan v1.4, R15")
    eta_rel_per_s: float = P(0.0, "1/s", doc="lineare relative Drift der Zustandseffizienz η je Lauf (Modenanpassung, Justage)", status="Vorschlag", source="Gesamtplan v1.4, R15")
    lo_rw_per_sqrt_s: float = P(0.0, "1/√s", doc="Irrfahrt der relativen LO-Leistung: Streuung der Änderung nach τ = q·√τ (0 = aus); z. B. 0,5 %/√h ≙ 8,3e-5", status="Annahme", source="Vorschlag M1 Laufplan")
    lo_sin_amp: float = P(0.0, "", doc="relative Amplitude einer periodischen LO-Schwankung (z. B. Klimaanlage); 0 = aus", status="Annahme", source="Vorschlag M1 Laufplan")
    lo_sin_period_s: float = P(1200.0, "s", doc="Periode der periodischen LO-Schwankung (Phase zufällig)", status="Annahme", source="Vorschlag M1 Laufplan")
    eta_rw_per_sqrt_s: float = P(0.0, "1/√s", doc="Irrfahrt der relativen Effizienz η (Modenanpassung, Justage); 0 = aus", status="Annahme", source="Vorschlag M1 Laufplan")
    eta_sin_amp: float = P(0.0, "", doc="relative Amplitude einer periodischen η-Schwankung; 0 = aus", status="Annahme", source="Vorschlag M1 Laufplan")
    eta_sin_period_s: float = P(1200.0, "s", doc="Periode der periodischen η-Schwankung", status="Annahme", source="Vorschlag M1 Laufplan")
    grid_dt_s: float = P(0.05, "s", doc="Stützstellenabstand der stochastischen Drift (linear interpoliert)", status="Vorschlag")
    monitor: bool = P(False, doc="zusätzlich externer, langsamer LO-Leistungsmesser (/blocks/<id>/monitor); Standard ist die Photodiode an ADQ32-Kanal B (lo_monitor)", status="Vorschlag")
    monitor_rate_Hz: float = P(10.0, "Hz", doc="Abtastrate des externen LO-Leistungsmessers", status="Annahme")
    monitor_noise_rel: float = P(1e-3, "", doc="relatives Rauschen des externen LO-Leistungsmessers", status="Annahme")


@dataclass
class OpticsCfg:
    """Wellenlänge und Detektoren (Entscheidung Herr Eilers zu Gesamtplan v1.6)."""
    wavelength_nm: float = P(1550.0, "nm", doc="Wellenlänge (Telekom-C-Band); Metadatum in session.json, digitizer.h5, PTU-Kopf", status="Vorschlag", source=DEC_16)
    herald_detector: str = P("snspd", doc="Preset des Herald-Detektors: snspd (Standard), ingaas_apd (Alternative) oder custom; setzt source.det_eff, R_dark, sigma_spd, det_dead_s, sofern dort nicht ausdrücklich angegeben", status="Vorschlag", source=DEC_16)
    homodyne_pd: str = P("InGaAs-PIN", doc="Photodioden des balancierten Homodyndetektors", status="Vorschlag", source=DEC_16)
    eta_hd_qe: float = P(0.95, "", doc="Quanteneffizienz der InGaAs-Homodyn-Photodioden; Literatur bis 99,5 % (Vahlbruch 2016) bzw. > 98 % bei 1510–1575 nm (Opt. Express 2022); source.eta = η_QE · η_Rest muss ≤ η_QE sein", status="Annahme", source="PRL 117, 110801 (2016); Opt. Express 30, 7, 10.1364/OE.447091")


@dataclass
class LoMonitorCfg:
    """LO-Leistungsmonitor: Photodiode (Strahlteiler am LO) an ADQ32-Kanal B (Entscheidung zu Gesamtplan v1.4).

    Kanal B wird im 2-Kanal-Modus mit demselben Trigger wie Kanal A aufgenommen; Recordlänge, Horizontal-Offset
    und Sample Skip sind je Kanal einstellbar [Spez., ADQ3 User Guide 21-2539, ADQDataAcquisitionParametersChannel,
    Kap. 5.2]. Der Mittelwert eines B-Records ist ein Maß für die LO-Leistung während dieses Records.
    """
    source: str = P("adq32_b", doc="adq32_b (Photodiode an Kanal B) oder off", status="Vorschlag", source="Entscheidung Herr Eilers (v1.5)")
    skip_factor: int = P(25, "", doc="Sample Skip für Kanal B (2,5 GS/s → 100 MS/s); Fenster wie Kanal A", status="Vorschlag", source=ADQ3_UG + ", Kap. 5.2")
    lsb_nominal: float = P(_PD_LEVELS["lsb_nominal"], "LSB", doc="Signal bei nominaler LO-Leistung über dem Dunkelwert; abgeleitet aus Photodiode, Dämpfer und LSB = 0,5 V/4096 (ohne Angabe in der Konfiguration neu berechnet)", status="Annahme")
    dark_lsb: float = P(_PD_LEVELS["dark_lsb"], "LSB", doc="Dunkelwert (Photodiode ohne Licht + DC-Offset); abgeleitet; im Labor mit LO aus messen", status="Annahme")
    noise_lsb: float = P(_PD_LEVELS["noise_lsb"], "LSB", doc="Rauschen je Sample (Photodiode über Dämpfer ⊕ 3 LSB ADC [Annahme]); abgeleitet", status="Annahme")
    rin_rel: float = P(1e-3, "", doc="relatives Intensitätsrauschen des LO je Record", status="Annahme")
    pd_model: str = P("PDA05CF2", doc="Photodiode des LO-Monitors (InGaAs, DC–150 MHz) mit 10-dB-Dämpfer vor Kanal B; Auskopplung TN1550R1A1 (99:1, Faser) bzw. BSF10-C (frei)", status="Vorschlag", source=DEC_16)
    pd_responsivity_A_W: float = P(_PD["responsivity_A_W"], "A/W", doc="Responsivität bei 1550 nm (Datenblatt: Spitze 1,04 A/W bei 1590 nm; Wert bei 1550 nm aus der Kurve geschätzt)", status="Annahme", source=PDA05)
    pd_gain_V_A: float = P(_PD["gain_V_A"], "V/A", doc="Transimpedanz an 50 Ω (10 kV/A an Hi-Z)", status="Spez.", source=PDA05)
    attenuator_dB: float = P(_PD["attenuator_dB"], "dB", doc="SMA-Dämpfer vor Kanal B: PDA-Ausgang bis 5 V an 50 Ω, ADQ32-Eingang Abs. Max. ±1,75 V", status="Vorschlag", source=DEC_16 + "; " + ADQ32_DS)
    p_tap_W: float = P(_PD["p_tap_W"], "W", doc="optische Leistung auf der Photodiode bei nominaler LO-Leistung (z. B. 1 % von 15 mW)", status="Annahme")
    pd_dark_V: float = P(_PD["dark_V"], "V", doc="Dunkeloffset der Photodiode am Ausgang (Spez. ±20 mV)", status="Annahme", source=PDA05)
    pd_noise_V: float = P(_PD["noise_V"], "V", doc="Rauschen der Photodiode (rms, 50 Ω, volle Bandbreite)", status="Spez.", source=PDA05)
    dc_offset_V: float = P(_PD["dc_offset_V"], "V", doc="programmierbarer DC-Offset von Kanal B (±0,25 V), nutzt den unipolaren Bereich", status="Vorschlag", source=ADQ32_DS)
    gain_rw_per_sqrt_s: float = P(0.0, "1/√s", doc="Irrfahrt der Monitor-Verstärkung (Teilerverhältnis, Polarisation, Photodiode); verfälscht die Normierung mit dem Monitor; 0 = aus", status="Annahme", source="Vorschlag M1 Laufplan")


@dataclass
class SourceCfg:
    R_herald: float = P(1000.0, "1/s", doc="Rate echter Heralds (CW, Poisson)", source=SYNC_REP)
    R_dark: float = P(HERALD_PRESETS["snspd"]["R_dark"], "1/s", doc="Dunkelzählrate des Herald-Detektors (falsche Heralds, Vakuum); Preset SNSPD: obere Grenze ID281 Standard bei 1300–1600 nm", status="Spez.", source=IDQ_281 + "; " + SQ_EOS)
    det_eff: float = P(HERALD_PRESETS["snspd"]["det_eff"], "", doc="Systemeffizienz des Herald-Detektors (Preset SNSPD 0,85; InGaAs-APD 0,20)", status="Spez.", source=IDQ_281 + "; " + SQ_EOS)
    herald_arm_T: float = P(HERALD_ARM_T, "", doc="Transmission des Herald-Arms vor dem Detektor (Filter, Fasereinkopplung)", status="Annahme")
    det_dead_s: float = P(HERALD_PRESETS["snspd"]["det_dead_s"], "s", doc="Totzeit/Erholzeit des Herald-Detektors (SNSPD typ. < 30 ns; InGaAs-APD 0,1–80 µs einstellbar, Preset 10 µs); Nachpulse nicht simuliert", status="Spez.", source=IDQ_281 + "; " + IDQ_QUBE)
    eta_herald: float = P(round(HERALD_PRESETS["snspd"]["det_eff"] * HERALD_ARM_T, 4), "", doc="Nachweiswahrscheinlichkeit im Herald-Arm = det_eff · herald_arm_T (ohne Angabe neu berechnet); bestimmt die Rate ungeheraldeter Signalphotonen R_herald·(1/η_h − 1)", source="Analyse Vergleich")
    eta: float = P(0.70, "", doc="Zustandseffizienz η = η_QE(InGaAs, optics.eta_hd_qe) · η_Rest (Optik, Modenanpassung, Visibility²) vor elektronischem Rauschen", source=SYNC_REP)
    heralded_state: str = P("fock1", doc="Zustand der geheraldeten Mode (fock1; fock2 nur als Test)", status="Vorschlag")
    mode_shape: str = P("double_exponential", doc="Temporale Mode: double_exponential f(t)=√γ·e^(−γ|t|) (CW-SPDC) oder gauss", status="Vorschlag")
    gamma: float = P(1 / 40e-9, "1/s", doc="Abklingrate der Mode (1/γ = 40 ns); bei gauss: 1/σ", source=SYNC_REP)
    sigma_spd: float = P(round(HERALD_PRESETS["snspd"]["sigma_spd"], 15), "s", doc="Jitter des Herald-Detektors (rms), gemeinsam für Harp und TRIG; Preset SNSPD 40 ps FWHM (ID281 Standard; Eos ≤ 15 ps), InGaAs-APD 150 ps FWHM", status="Spez.", source=IDQ_281 + "; " + IDQ_QUBE)


@dataclass
class PhaseCfg:
    mode: str = P("random", doc="random (phasengemittelt), fixed, ramp (Sägezahn), steps", status="Vorschlag")
    theta0: float = P(0.0, "rad", doc="Phase bei fixed bzw. Startphase von ramp/steps", status="Vorschlag")
    ramp_Hz: float = P(10.0, "Hz", doc="Sägezahnfrequenz der Phasenrampe (2π je Periode)", status="Annahme")
    steps: list = P([0.0, 0.7853981633974483, 1.5707963267948966, 2.356194490169959], "rad", doc="Phasenwerte bei steps", status="Vorschlag")
    dwell_s: float = P(0.25, "s", doc="Verweildauer je Phasenstufe", status="Vorschlag")
    marker_bit: int = P(2, "", doc="Harp-Marker-Bitmaske für Rampen-/Stufenbeginn (0 = aus)", status="Vorschlag")


@dataclass
class HarpCfg:
    model: str = P("HydraHarp 500 S", doc="HydraHarp 500 S (4–8 Kanäle) oder M (8–16 Kanäle); Festlegung: S", status="Spez.", source=HH500_MANUAL)
    n_channels: int = P(4, "", doc="Anzahl Eingangskanäle CH1..CHn (S: 4..8, M: 8..16), zusätzlich der Sync-Eingang (Kanal 0)", status="Spez.", source=HH500_MANUAL)
    resolution: float = P(1e-12, "s", doc="T2-Auflösung (fest 1 ps)", status="Spez.", source=HH500_MANUAL)
    jitter_rms: float = P(2.5e-12, "s", doc="Einzelmessungs-Zeitunsicherheit typ. (rms)", status="Spez.", source=HH500_MANUAL)
    trigger: str = P("ETR", doc="Eingangstrigger ETR (Totzeit < 680 ps) oder CFD (< 6,8 ns)", status="Spez.", source=HH500_MANUAL)
    tdead: float = P(0.0, "s", doc="programmierbare Zusatz-Totzeit (0 = native Totzeit)", status="Spez.", source="HydraHarp 500 Manual, Kap. 6.1")
    herald_channel: int = P(1, "", doc="CH1: Herald-Detektor (Kopie über Fan-out an ADQ32 TRIG)", status="Vorschlag", source="Gesamtplan v1.5, Kanalbelegung")
    trigger_copy_channel: int = P(2, "", doc="CH2: Kopie der ADQ32-Triggerereignisse (Pulsgenerator an ADQ32-GPIO); 0 = aus", status="Vorschlag", source="Gesamtplan v1.5, Kanalbelegung")
    herald2_channel: int = P(3, "", doc="CH3: reserviert für zweiten Herald-Detektor bzw. PNR (nicht simuliert); 0 = keine Reservierung", status="Vorschlag", source="Gesamtplan v1.5, Kanalbelegung")
    random_tag_channel: int = P(4, "", doc="CH4 (Hilfskanal): Kopie des Zufallstriggers (Lauf random, Option AP20)", status="Vorschlag", source="Gesamtplan v1.5, Kanalbelegung")
    state_tag_channel: int = P(4, "", doc="CH4 (Hilfskanal): Trigger getriggerter Testzustände (nie im selben Lauf wie Zufallstrigger)", status="Vorschlag", source="Gesamtplan v1.5, Kanalbelegung")
    marker_timing_channel: int = P(0, "", doc="Timing-Kanal der Sync-Marker, falls markers.on_timing_channel: 0 = Sync-Eingang (in T2 vollwertiger Zeitstempelkanal, Teiler aus)", status="Vorschlag", source=HH500_T2)
    sync_divider: int = P(1, "", doc="Sync-Teiler; in T2 bei Nutzung des Sync-Eingangs als Kanal 1 (\"None\") [Spez.]", status="Spez.", source=HH500_T2)
    run_marker_bit: int = P(1, "", doc="Marker-Eingang M1 (Bit 1): Laufmarke bei Laufbeginn und -ende (v1.0 per Software; optional ADQ32-GPIOC0 → 3,3-V-Puffer → M1); 0 = aus", status="Vorschlag", source="Gesamtplan v1.6, Entscheidung 17.10.2026")
    d_TC: float = P(30e-9, "s", doc="Laufzeit ADQ32-GPIO → Harp CH2 (Kabel, Abschwächer)", status="Annahme")
    channel_offsets: dict = P({}, "s", doc="Eingangs-Offsets je Kanal {Kanal: s}, Bereich ±100 ns", status="Spez.", source=HH500_MANUAL)
    d_H: float = P(85.3e-9, "s", doc="Laufzeit Herald-Detektor → Harp", source=SYNC_REP)
    d_RH: float = P(10e-9, "s", doc="Laufzeit Zufalls-/Zustandstrigger-Generator → Harp", status="Annahme")
    marker_bit: int = P(4, "", doc="Marker-Eingang M3 (Bit 4) für Sync-Marker, nur falls nicht auf einem Timing-Kanal (Rückfall); Störmarker-Modell", status="Vorschlag")
    marker_error_rms: float = P(30e-9, "s", doc="Streuung des Marker-Zeitfehlers (Spez.: Fehler < 100 ns in T2; Gaußmodell = Annahme)", source=SYNC_REP)
    marker_offset: float = P(20e-9, "s", doc="konstante Marker-Latenz", source=SYNC_REP)
    marker_error_max: float = P(100e-9, "s", doc="Grenze des Marker-Zeitfehlers in T2 (Modell wird hier abgeschnitten)", status="Spez.", source=HH500_MANUAL)
    marker_holdoff: float = P(0.0, "s", doc="Marker-Hold-off (unterdrückt Folge-Marker)", status="Spez.", source="HydraHarp 500 Manual, Kap. 6.3")
    R_spurious: float = P(0.2, "1/s", doc="Störmarker (Glitches) an der Harp", source=SYNC_REP)


@dataclass
class DigitizerCfg:
    model: str = P("ADQ32", doc="Digitizer-Modell", status="Spez.", source="ED-6")
    form_factor: str = P("PCIe", doc="Bauform PCIe (ED-14) oder USB", status="Spez.", source=ADQ32_DS)
    clock_option: str = P("S2G5", doc="S2G5 (2,5 GS/s je Kanal, 5 GS/s 1-Kanal) oder S2G0 (2/4 GS/s); Festlegung ED-14: S2G5", status="Spez.", source=ADQ32_DS)
    bandwidth_option: str = P("BW1G0", doc="Analogbandbreite BW1G0 (1 GHz) oder BW2G5 (2,5 GHz); Festlegung ED-14: BW1G0", status="Spez.", source=ADQ32_DS)
    frontend_filter: str = P("first_order", doc="Modell der Analogbandbreite: first_order (Tiefpass 1. Ordnung, −3 dB bei der Bandbreite) oder none", status="Annahme")
    channel_mode: int = P(2, "", doc="2-Kanal- oder 1-Kanal-Modus", status="Spez.", source=ADQ32_DS)
    skip_factor: int = P(1, "", doc="Sample Skip (ohne Tiefpass, User Guide 5.2)", status="Spez.", source=ADQ3_UG)
    record_length: int = P(5000, "Samples", doc="Recordlänge (2 µs bei 2,5 GS/s; Off-Herald-Fenster)", status="Vorschlag")
    pretrigger: int = P(2000, "Samples", doc="Pretrigger (negativer horizontal_offset); 2-Kanal: Schritte 8, max 16 360", status="Vorschlag")
    rearm: float = P(20e-9, "s", doc="Rearm-Zeit (max.)", status="Spez.", source=ADQ32_DS)
    trig_res_ref: float = P(50e-12, "s", doc="TRIG-Zeitauflösung bei 2,5 GS/s (skaliert linear mit der Abtastrate)", status="Spez.", source=ADQ32_DS)
    sync_res_ref: float = P(3.2e-9, "s", doc="SYNC-Zeitauflösung bei 2,5 GS/s (skaliert linear)", status="Spez.", source=ADQ32_DS)
    trig_max_rate: float = P(10e6, "1/s", doc="max. Pulsfolgefrequenz am TRIG-Eingang", status="Spez.", source=ADQ32_DS)
    time_unit_div: int = P(8, "", doc="time_unit = 1/(f_ADC·time_unit_div); Wert von time_unit nicht dokumentiert", status="Annahme")
    adc_bits: int = P(12, "bit", doc="ADC-Auflösung", status="Spez.", source=ADQ32_DS)
    lsb_per_unit: float = P(80.0, "LSB", doc="ADC-LSB je √(Vakuumvarianz·2) – Verstärkung", source=SYNC_REP)
    adc_offset: float = P(3.0, "LSB", doc="ADC-Nullpunktfehler", status="Annahme")
    clearance_dB: float = P(15.0, "dB", doc="Shot-Noise-Clearance (Vakuum / elektronisches Rauschen)", source=SYNC_REP)
    d_T: float = P(42.7e-9, "s", doc="Laufzeit Herald-Detektor → TRIG", source=SYNC_REP)
    d_S: float = P(120e-9, "s", doc="Laufzeit Modenzentrum → Analogeingang", source=SYNC_REP)
    d_RT: float = P(12e-9, "s", doc="Laufzeit Zufalls-/Zustandstrigger-Generator → TRIG", status="Annahme")
    d_MD: float = P(23e-9, "s", doc="Laufzeit Markerquelle → SYNC", source=SYNC_REP)
    p_miss_marker: float = P(0.001, "", doc="Anteil verlorener Marker am Digitizer", source=SYNC_REP)
    trig_jitter: float = P(20e-12, "s", doc="Jitter des TRIG-/SYNC-Eingangs", status="Annahme")
    header_reports_trigger_source: bool = P(False, doc="meldet der Record-Header den auslösenden Eingang? (unbekannt → false; Kennung über Harp)", status="Annahme")
    crosstalk_amp: float = P(0.0, "Vakuum-σ", doc="Amplitude eines Trigger-Übersprechens auf den Analogkanal (Fehlerbild R10)", status="Vorschlag")
    crosstalk_tau: float = P(15e-9, "s", doc="Abklingzeit des Übersprechens", status="Annahme")
    trigger_copy_latency: float = P(10e-9, "s", doc="Latenz Triggerereignis → Pulsgenerator-Ausgang (GPIO)", status="Annahme")
    pulse_gen_granularity: int = P(32, "Samples", doc="Zeitraster des Pulsgenerators in Samples der Basisabtastrate (5 GHz bei S2G5 → 6,4 ns)", status="Spez.", source=ADQ3_UG + ", Kap. 7.2 (Beispiel ADQ32)")
    crosstalk_delay: float = P(77.3e-9, "s", doc="Verzögerung des Übersprechens nach dem TRIG-Puls (Default d_S − d_T: am Modenort, ungünstigster Fall)", status="Annahme")


@dataclass
class ClockCfg:
    eps: float = P(2.0e-6, "", doc="relative Frequenzabweichung Digitizer gegen Harp (ADQ32 intern ±3 ppm Spez.; HydraHarp OCXO ±300 ppb)", source=SYNC_REP)
    t_arm_s: float = P(0.137, "s", doc="wahre Zeit, zu der der Digitizer-Zähler startet", source=SYNC_REP)
    rw_D: float = P(0.0, "s²/s", doc="Diffusionskonstante der Phasen-Irrfahrt (0 = aus)", source=SYNC_REP)
    shared_10MHz: bool = P(True, doc="gemeinsame 10-MHz-Referenz (ED-7): ε = 0, keine Irrfahrt", status="Vorschlag", source="ED-7")
    grid_dt: float = P(1e-3, "s", doc="Stützstellenabstand der Irrfahrt", status="Vorschlag")


@dataclass
class MarkerCfg:
    f_marker: float = P(1000.0, "Hz", doc="Rate der Sync-Marker", source=SYNC_REP)
    p_omit: float = P(0.10, "", doc="pseudozufällige Auslassung (Muster für die Zuordnung)", source=SYNC_REP)
    on_timing_channel: bool = P(True, doc="Sync-Marker auf einem Timing-Kanal (Standard: Sync-Eingang, 1 ps) statt auf einem Marker-Eingang (< 100 ns)", status="Vorschlag", source="ED-7; " + HH500_T2)
    sigma_tc: float = P(5e-12, "s", doc="Streuung, wenn der Marker auf einem Timing-Kanal liegt", source=SYNC_REP)
    d_MH: float = P(15e-9, "s", doc="Laufzeit Markerquelle → Harp", source=SYNC_REP)


@dataclass
class BlockCfg:
    id: str = P("heralded", doc="eindeutiger Blockname", status="Vorschlag")
    kind: str = P("heralded", doc="heralded | random | free_running | vacuum | herald_blocked | electronic | state", status="Vorschlag")
    duration_s: float = P(5.0, "s", doc="Messdauer des Laufs", status="Vorschlag")
    run_id: str = P("", doc="Lauf-ID; leer = automatisch <session.id>-R<nn>", status="Vorschlag")
    random_rate: float = P(0.0, "1/s", doc="Zufallstrigger (Poisson): Lauftrigger (random) bzw. eingestreut in heralded (Option, ED-12)", status="Vorschlag")
    periodic_rate: float = P(2000.0, "1/s", doc="interne periodische Trigger (vacuum, electronic, free_running)", status="Vorschlag")
    state: dict = P({}, doc="Testzustand bei kind=state: {type: coherent|thermal|fock|cat|vacuum|fock1, ...}", status="Vorschlag")
    state_rate: float = P(1000.0, "1/s", doc="Rate getriggerter Testzustände (periodisch)", status="Vorschlag")
    phase: dict = P({}, doc="blockeigene Überschreibung von phase.*", status="Vorschlag")
    crosstalk_amp: float | None = P(None, doc="blockeigene Überschreibung von digitizer.crosstalk_amp", status="Vorschlag")
    lo_scale: float = P(1.0, "", doc="Faktor auf die LO-Leistung in diesem Lauf (Sprung; zusätzlich zu drift.lo_rel_per_s)", status="Vorschlag")
    eta_scale: float = P(1.0, "", doc="Faktor auf η in diesem Lauf (Sprung; zusätzlich zu drift.eta_rel_per_s)", status="Vorschlag")


# Laufplan M1 (Vorschlag „Vorschläge M1“, übernommen als Default in Gesamtplan v1.6), Echtzeit bei 1 kHz Herald:
# K-V 20 s @ 10 kHz (Anfang, nach jedem 3. Zyklus, Ende), Dunkellauf 10 s @ 10 kHz (LO aus; 0.8.0: η_el im
# Effizienzbudget), K-H 60 s, 6 × (A 60 s, B 30 s @ 2 kHz) + A.
M1_PLAN = dict(T_A=60.0, T_B=30.0, n_cycles=6, T_KV=20.0, kv_every=3, T_KH=60.0, kh_end=False, rate_B=2000.0,
               rate_KV=10000.0, T_dark=10.0, rate_dark=10000.0)
SIM_TIME_SCALE = 1 / 90     # Standardsitzung im Zeitraffer (≈ 8 s statt 12 min); Echtzeit: config/laufplan_m1.yaml


def default_blocks(time_scale: float = SIM_TIME_SCALE) -> list[dict]:
    """Standard-Messablauf = Laufplan M1 (ED-12, Gesamtplan v1.6) mit allen Dauern × ``time_scale``.

    K-V → Dunkel → K-H → A → B → … (K-V nach jedem 3. Zyklus) → A → K-V; B freilaufend (2 kHz), K-V periodisch (10 kHz).
    Die Echtzeitfassung steht in ``config/laufplan_m1.yaml`` (``hhd-sim plan``); Lauf B mit Zufallstrigger
    (config/random_b.yaml), Off-Herald-Fenster und eingestreute Zufallstrigger bleiben Optionen.
    """
    from .plan import make_plan
    blocks = make_plan(M1_PLAN)
    for b in blocks:
        b["duration_s"] = round(b["duration_s"] * time_scale, 6)
    return blocks


BLOCK_KINDS = ("heralded", "random", "free_running", "vacuum", "herald_blocked", "electronic", "state")
BANDWIDTH = {"BW1G0": 1.0e9, "BW2G5": 2.5e9}


@dataclass
class SimConfig:
    session: SessionCfg = field(default_factory=SessionCfg)
    source: SourceCfg = field(default_factory=SourceCfg)
    phase: PhaseCfg = field(default_factory=PhaseCfg)
    harp: HarpCfg = field(default_factory=HarpCfg)
    digitizer: DigitizerCfg = field(default_factory=DigitizerCfg)
    clock: ClockCfg = field(default_factory=ClockCfg)
    markers: MarkerCfg = field(default_factory=MarkerCfg)
    drift: DriftCfg = field(default_factory=DriftCfg)
    lo_monitor: LoMonitorCfg = field(default_factory=LoMonitorCfg)
    optics: OpticsCfg = field(default_factory=OpticsCfg)
    blocks: list[BlockCfg] = field(default_factory=lambda: [BlockCfg(**b) for b in default_blocks()])

    # ---------------------------------------------------------------- abgeleitete Größen (ADQ32)
    @property
    def f_adc(self) -> float:
        base = {"S2G5": 2.5e9, "S2G0": 2.0e9}[self.digitizer.clock_option]
        return base * (2 if self.digitizer.channel_mode == 1 else 1)

    @property
    def fs(self) -> float:
        """effektive Abtastrate nach Sample Skip."""
        return self.f_adc / self.digitizer.skip_factor

    @property
    def time_unit(self) -> float:
        return 1.0 / (self.f_adc * self.digitizer.time_unit_div)

    @property
    def timing_scale(self) -> float:
        """Zeitauflösungen gelten bei 2,5 GS/s (2-Kanal) bzw. 5 GS/s (1-Kanal) und skalieren linear [Spez.].
        Dass Sample Skip die Trigger-Auflösung nicht ändert, ist eine Annahme."""
        ref = 2.5e9 * (2 if self.digitizer.channel_mode == 1 else 1)
        return ref / self.f_adc

    @property
    def trig_res(self) -> float:
        return self.digitizer.trig_res_ref * self.timing_scale

    @property
    def sync_res(self) -> float:
        return self.digitizer.sync_res_ref * self.timing_scale

    @property
    def bandwidth(self) -> float:
        return BANDWIDTH.get(self.digitizer.bandwidth_option, float("nan"))

    @property
    def f_base(self) -> float:
        """Basisabtastrate des ADC (1-Kanal-Rate): 5 GHz (S2G5) bzw. 4 GHz (S2G0)."""
        return {"S2G5": 5.0e9, "S2G0": 4.0e9}[self.digitizer.clock_option]

    @property
    def pulse_gen_step(self) -> float:
        """Zeitraster des ADQ32-Pulsgenerators (6,4 ns bei S2G5) [Spez. für ADQ32 bei 5 GHz; S2G0 Annahme]."""
        return self.digitizer.pulse_gen_granularity / self.f_base

    @property
    def lo_b_enabled(self) -> bool:
        return self.lo_monitor.source == "adq32_b"

    @property
    def lo_b_record_length(self) -> int:
        return self.digitizer.record_length // self.lo_monitor.skip_factor

    @property
    def lo_b_pretrigger(self) -> int:
        return self.digitizer.pretrigger // self.lo_monitor.skip_factor

    def channel_roles(self, kind: str | None = None) -> dict[str, str]:
        """Belegung der HydraHarp-Eingänge {Kanal: Rolle}; Kanal 0 = Sync-Eingang. ``kind``: Laufart."""
        h = self.harp
        roles = {str(h.herald_channel): "herald"}
        if h.trigger_copy_channel:
            roles[str(h.trigger_copy_channel)] = "adq32_trigger_copy"
        if h.herald2_channel:
            roles[str(h.herald2_channel)] = "herald2_reserved"
        aux = {"state": "state_tag", "random": "random_tag"}.get(kind or "", "")
        if kind == "heralded":
            aux = "random_tag (nur bei random_rate > 0)"
        if aux.startswith("state"):
            roles[str(h.state_tag_channel)] = aux
        elif aux:
            roles[str(h.random_tag_channel)] = aux
        else:
            roles.setdefault(str(h.random_tag_channel), "aux (Zufalls-/Zustandstrigger, sonst Reserve)")
        if self.markers.on_timing_channel:
            roles[str(h.marker_timing_channel)] = "sync_marker"
        return dict(sorted(roles.items(), key=lambda kv: int(kv[0])))

    def marker_roles(self) -> dict[str, str]:
        h, roles = self.harp, {}
        if h.run_marker_bit:
            roles[str(h.run_marker_bit)] = "run_marker"
        if self.phase.marker_bit:
            roles[str(self.phase.marker_bit)] = "phase_ref"
        if not self.markers.on_timing_channel:
            roles[str(h.marker_bit)] = "sync_marker"
        return roles

    @property
    def calibration_id(self) -> str:
        return f"{self.session.campaign}-{self.session.id}-cal"

    def run_ids(self) -> list[str]:
        return [b.run_id or f"{self.session.id}-R{i + 1:02d}" for i, b in enumerate(self.blocks)]

    @property
    def harp_dead(self) -> float:
        native = 680e-12 if self.harp.trigger == "ETR" else 6.8e-9
        return max(native, self.harp.tdead)

    @property
    def clearance(self) -> float:
        return 10 ** (self.digitizer.clearance_dB / 10)

    # ---------------------------------------------------------------- Prüfung
    def validate(self) -> list[str]:
        """Prüft gegen Gerätegrenzen. Fehler → ConfigError; Rückgabe: Warnungen."""
        e: list[str] = []
        w: list[str] = []
        h, d, s = self.harp, self.digitizer, self.source
        if h.model not in ("HydraHarp 500 S", "HydraHarp 500 M"):
            e.append("harp.model: 'HydraHarp 500 S' oder 'HydraHarp 500 M'")
        lo, hi = (4, 8) if h.model.endswith("S") else (8, 16)
        if not lo <= h.n_channels <= hi:
            e.append(f"harp.n_channels: {h.model} hat {lo}..{hi} Kanäle")
        if h.resolution != 1e-12:
            e.append("harp.resolution: T2-Auflösung ist fest 1 ps")
        if h.trigger not in ("ETR", "CFD"):
            e.append("harp.trigger: ETR oder CFD")
        # Zufalls- und Zustandstrigger dürfen sich den Hilfskanal teilen (nie im selben Lauf)
        aux = {h.random_tag_channel, h.state_tag_channel}
        chans = [h.herald_channel] + [c for c in (h.trigger_copy_channel, h.herald2_channel) if c] + sorted(aux)
        if self.markers.on_timing_channel:
            chans.append(h.marker_timing_channel)
        if len(set(chans)) != len(chans):
            e.append("harp: Kanalbelegung doppelt")
        for c in chans:
            if not 0 <= c <= h.n_channels:
                e.append(f"harp: Kanal {c} außerhalb 0 (Sync)..{h.n_channels}")
        if h.herald_channel == 0 or 0 in aux:
            w.append("harp: Herald bzw. Hilfstrigger am Sync-Eingang (möglich in T2, aber Sync-Marker dann auf CH)")
        if 0 in chans and h.sync_divider != 1:
            e.append("harp.sync_divider: in T2 muss der Sync-Teiler aus sein (1), wenn der Sync-Eingang Zeitstempel liefert")
        if self.markers.on_timing_channel and h.marker_timing_channel == 0 and self.markers.f_marker > 85e6:
            e.append("markers.f_marker: max. Sync-Rate ohne Teiler 85 MHz")
        if self.markers.on_timing_channel and self.markers.f_marker > 1e6:
            w.append("markers.f_marker > 1 MHz: hoher Anteil am USB-Durchsatz (typ. 85 Mcps); 10 MHz gehört an Ref-In, nicht an einen Kanal")
        mbits = [b for b in (h.run_marker_bit, self.phase.marker_bit) if b]
        if not self.markers.on_timing_channel:
            mbits.append(h.marker_bit)
        if any(b not in (1, 2, 4, 8) for b in mbits + [h.marker_bit]) or h.run_marker_bit not in (0, 1, 2, 4, 8):
            e.append("Marker-Bit: 1, 2, 4 oder 8 (vier Marker-Eingänge)")
        if len(set(mbits)) != len(mbits):
            e.append("harp: Marker-Eingänge doppelt belegt")
        for c, off in h.channel_offsets.items():
            if abs(float(off)) > 100e-9:
                e.append(f"harp.channel_offsets[{c}]: Bereich ±100 ns")
        if self.phase.marker_bit not in (0, 1, 2, 4, 8):
            e.append("Marker-Bit: 1, 2, 4 oder 8 (vier Marker-Eingänge)")
        if not self.markers.on_timing_channel and self.markers.f_marker > 1 / 100e-9:
            e.append("markers.f_marker: Marker-Pulse und -Pausen je > 50 ns (max. 10 MHz)")
        if d.model != "ADQ32":
            e.append("digitizer.model: nur ADQ32")
        if d.form_factor not in ("PCIe", "USB"):
            e.append("digitizer.form_factor: PCIe oder USB")
        if d.clock_option not in ("S2G5", "S2G0"):
            e.append("digitizer.clock_option: S2G5 oder S2G0")
        if d.channel_mode not in (1, 2):
            e.append("digitizer.channel_mode: 1 oder 2")
        if d.skip_factor < 1:
            e.append("digitizer.skip_factor ≥ 1")
        step, pmax = (8, 16360) if d.channel_mode == 2 else (16, 16336)
        if d.pretrigger % step or not 0 <= d.pretrigger <= pmax:
            e.append(f"digitizer.pretrigger: 0..{pmax} in Schritten von {step}")
        if not 2 <= d.record_length < 2 ** 32:
            e.append("digitizer.record_length: 2 .. 2^32−1")
        if d.pretrigger >= d.record_length:
            e.append("digitizer.pretrigger < record_length")
        if d.bandwidth_option not in BANDWIDTH:
            e.append("digitizer.bandwidth_option: BW1G0 oder BW2G5")
        if d.frontend_filter not in ("first_order", "none"):
            e.append("digitizer.frontend_filter: first_order oder none")
        if d.adc_bits != 12:
            e.append("digitizer.adc_bits: ADQ32 hat 12 bit")
        lm = self.lo_monitor
        if lm.source not in ("adq32_b", "off"):
            e.append("lo_monitor.source: adq32_b oder off")
        if self.lo_b_enabled:
            if d.channel_mode != 2:
                e.append("lo_monitor: Kanal B nur im 2-Kanal-Modus (digitizer.channel_mode = 2)")
            if lm.skip_factor < 1 or d.record_length % lm.skip_factor or d.pretrigger % lm.skip_factor:
                e.append("lo_monitor.skip_factor ≥ 1 und Teiler von record_length und pretrigger")
            elif self.lo_b_pretrigger % 8:
                e.append("lo_monitor: Pretrigger von Kanal B in Schritten von 8 Samples")
            lo_max = 1 + max(0.0, self.drift.lo_rel_per_s) * sum(b.duration_s + self.session.gap_s for b in self.blocks)
            lo_max *= max([b.lo_scale for b in self.blocks] or [1.0])
            if lm.dark_lsb + lm.lsb_nominal * lo_max + 5 * lm.noise_lsb > 2 ** (d.adc_bits - 1) - 1:
                w.append("lo_monitor: Kanal B übersteuert bei maximaler LO-Leistung (Verstärkung/DC-Offset anpassen)")
        if abs(self.clock.eps) > 3e-6 and not self.clock.shared_10MHz:
            w.append("clock.eps > ±3 ppm (ADQ32-Spezifikation der internen Referenz)")
        if d.form_factor == "USB":
            w.append("digitizer.form_factor=USB: max. 1,9 GByte/s; Architektur B mit voller Rate nicht möglich")
        if not 0 < s.eta <= 1 or not 0 < s.eta_herald <= 1:
            e.append("source.eta, eta_herald in (0, 1]")
        o = self.optics
        if o.herald_detector not in (*HERALD_PRESETS, "custom"):
            e.append("optics.herald_detector: snspd, ingaas_apd oder custom")
        if not 800 <= o.wavelength_nm <= 1700:
            w.append("optics.wavelength_nm außerhalb 800–1700 nm (InGaAs-Komponenten des Defaults)")
        if not 0 < o.eta_hd_qe <= 1 or s.eta > o.eta_hd_qe + 1e-12:
            e.append("source.eta ≤ optics.eta_hd_qe ≤ 1 (η enthält die Quanteneffizienz der Homodyn-Photodioden)")
        if not 0 < s.det_eff <= 1 or not 0 < s.herald_arm_T <= 1 or s.det_dead_s < 0:
            e.append("source.det_eff, herald_arm_T in (0, 1], det_dead_s ≥ 0")
        if s.det_dead_s * (s.R_herald + s.R_dark) > 0.05:
            w.append("Herald-Detektor: Totzeit·Rate > 5 % – Verluste und Nachpulse beachten (InGaAs-APD)")
        if self.lo_b_enabled:
            lev = lo_monitor_levels(lm.pd_responsivity_A_W, lm.pd_gain_V_A, lm.attenuator_dB, lm.p_tap_W, lm.pd_dark_V,
                                    lm.pd_noise_V, lm.dc_offset_V, bits=d.adc_bits)
            if lev["v_max_at_adc"] > 1.75:
                e.append(f"lo_monitor: Photodiode kann {lev['v_max_at_adc']:.2f} V an Kanal B liefern > Abs. Max. ±1,75 V "
                         "(Dämpfer ≥ 10 dB)")
            if abs(lm.dc_offset_V) > 0.25:
                e.append("lo_monitor.dc_offset_V: ±0,25 V")
            if lm.dark_lsb - 5 * lm.noise_lsb < -2 ** (d.adc_bits - 1):
                w.append("lo_monitor: Dunkelwert unter dem Messbereich (DC-Offset anpassen)")
        if s.heralded_state not in ("fock1", "fock2"):
            e.append("source.heralded_state: fock1 oder fock2")
        if s.mode_shape not in ("double_exponential", "gauss"):
            e.append("source.mode_shape: double_exponential oder gauss")
        ids = [b.id for b in self.blocks]
        if len(set(ids)) != len(ids):
            e.append("blocks: id doppelt")
        rids = self.run_ids()
        if len(set(rids)) != len(rids):
            e.append("blocks: run_id doppelt")
        if not self.session.id or not self.session.campaign:
            e.append("session.id und session.campaign dürfen nicht leer sein")
        dr = self.drift
        T_tot = sum(b.duration_s + self.session.gap_s for b in self.blocks)
        if 1 + min(0.0, dr.lo_rel_per_s) * T_tot <= 0.05 or 1 + min(0.0, dr.eta_rel_per_s) * T_tot <= 0.05:
            e.append("drift: negative Drift macht LO-Leistung bzw. η in der Sitzung ≤ 5 %")
        if min(dr.lo_rw_per_sqrt_s, dr.eta_rw_per_sqrt_s, self.lo_monitor.gain_rw_per_sqrt_s) < 0:
            e.append("drift: Irrfahrt-Koeffizienten ≥ 0")
        if abs(dr.lo_sin_amp) >= 0.5 or abs(dr.eta_sin_amp) >= 0.5 or dr.lo_sin_period_s <= 0 or dr.eta_sin_period_s <= 0:
            e.append("drift: |sin_amp| < 0,5 und sin_period_s > 0")
        if dr.grid_dt_s <= 0:
            e.append("drift.grid_dt_s > 0")
        if dr.monitor_rate_Hz <= 0 or dr.monitor_noise_rel < 0:
            e.append("drift.monitor_rate_Hz > 0, monitor_noise_rel ≥ 0")
        for b in self.blocks:
            if b.lo_scale <= 0 or b.eta_scale <= 0:
                e.append(f"blocks[{b.id}]: lo_scale, eta_scale > 0")
            if b.kind not in BLOCK_KINDS:
                e.append(f"blocks[{b.id}].kind unbekannt: {b.kind}")
            if b.duration_s <= 0:
                e.append(f"blocks[{b.id}].duration_s > 0")
            if b.kind == "state" and not b.state:
                e.append(f"blocks[{b.id}]: kind=state braucht state")
            trig_rate = (s.R_herald + s.R_dark if b.kind in ("heralded", "herald_blocked") else 0) + b.random_rate
            if trig_rate > d.trig_max_rate:
                e.append(f"blocks[{b.id}]: Triggerrate über 10 MHz (TRIG)")
        if self.phase.mode not in ("random", "fixed", "ramp", "steps"):
            e.append("phase.mode: random, fixed, ramp oder steps")
        if e:
            raise ConfigError("; ".join(e))
        return w

    # ---------------------------------------------------------------- Ein-/Ausgabe
    def to_dict(self) -> dict:
        return asdict(self)

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=True)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=1, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "SimConfig":
        d = dict(d or {})
        kw: dict[str, Any] = {}
        groups = {f.name: f for f in fields(cls)}
        unknown = set(d) - set(groups)
        if unknown:
            raise ConfigError(f"unbekannte Abschnitte: {sorted(unknown)}")
        for name, f in groups.items():
            if name not in d:
                continue
            if name == "blocks":
                kw["blocks"] = [_build(BlockCfg, b, f"blocks[{i}]") for i, b in enumerate(d["blocks"])]
            else:
                kw[name] = _build(f.default_factory().__class__, d[name], name)  # type: ignore[misc]
        cfg = cls(**kw)
        cfg._apply_derived(d)
        return cfg

    def _apply_derived(self, d: dict) -> None:
        """Presets und abgeleitete Größen: nur Felder, die in ``d`` nicht ausdrücklich stehen."""
        src_given = set((d.get("source") or {}).keys())
        lm_given = set((d.get("lo_monitor") or {}).keys())
        pre = HERALD_PRESETS.get(self.optics.herald_detector)
        if pre:
            for k, v in pre.items():
                if k not in src_given:
                    setattr(self.source, k, float(v))
        if "eta_herald" not in src_given:
            self.source.eta_herald = round(self.source.det_eff * self.source.herald_arm_T, 4)
        lm = self.lo_monitor
        lev = lo_monitor_levels(lm.pd_responsivity_A_W, lm.pd_gain_V_A, lm.attenuator_dB, lm.p_tap_W, lm.pd_dark_V,
                                lm.pd_noise_V, lm.dc_offset_V, vpp=0.5, bits=self.digitizer.adc_bits)
        for k in ("lsb_nominal", "dark_lsb", "noise_lsb"):
            if k not in lm_given:
                setattr(lm, k, lev[k])

    @classmethod
    def load(cls, path: str | Path) -> "SimConfig":
        text = Path(path).read_text(encoding="utf-8")
        data = json.loads(text) if str(path).endswith(".json") else yaml.safe_load(text)
        return cls.from_dict(data)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json() if str(path).endswith(".json") else self.to_yaml(), encoding="utf-8")

    def block(self, block_id: str) -> BlockCfg:
        for b in self.blocks:
            if b.id == block_id:
                return b
        raise KeyError(block_id)

    def phase_for(self, b: BlockCfg) -> PhaseCfg:
        p = copy.deepcopy(self.phase)
        for k, v in (b.phase or {}).items():
            if not hasattr(p, k):
                raise ConfigError(f"blocks[{b.id}].phase.{k} unbekannt")
            setattr(p, k, v)
        return p


def _build(klass, data: dict | None, where: str):
    data = dict(data or {})
    names = {f.name for f in fields(klass)}
    unknown = set(data) - names
    if unknown:
        raise ConfigError(f"{where}: unbekannte Parameter {sorted(unknown)}")
    for f in fields(klass):
        if f.name in data and data[f.name] is not None:
            dv = f.default if f.default is not MISSING else None
            if isinstance(dv, bool):
                data[f.name] = bool(data[f.name])
            elif isinstance(dv, float):
                data[f.name] = float(data[f.name])
            elif isinstance(dv, int):
                data[f.name] = int(data[f.name])
    return klass(**data)


# ============================================================================ Parametertabelle
def parameter_rows(cfg: SimConfig | None = None) -> list[dict]:
    cfg = cfg or SimConfig()
    rows = []
    for gname in ("session", "source", "phase", "harp", "digitizer", "clock", "markers", "drift", "lo_monitor", "optics"):
        grp = getattr(cfg, gname)
        for f in fields(grp):
            md = f.metadata
            rows.append(dict(name=f"{gname}.{f.name}", value=getattr(grp, f.name), unit=md.get("unit", ""),
                             status=md.get("status", ""), source=md.get("source", ""), doc=md.get("doc", "")))
    for f in fields(BlockCfg):
        md = f.metadata
        default = f.default if f.default is not MISSING else f.default_factory()  # type: ignore[misc]
        rows.append(dict(name=f"blocks[].{f.name}", value=default, unit=md.get("unit", ""),
                         status=md.get("status", ""), source=md.get("source", ""), doc=md.get("doc", "")))
    return rows


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:.6g}"
    if isinstance(v, (list, dict)):
        return json.dumps(v)
    return str(v)


def parameter_markdown(cfg: SimConfig | None = None) -> str:
    out = ["# Parameter des Simulators (Defaults)", "",
           "Erzeugt mit `hhd-sim params --markdown` aus `hhd/sim/config.py`. "
           "Status: **Spez.** = Herstellerunterlage, **Vorbericht** / Annahme / Vorschlag siehe Modulkopf.", "",
           "| Parameter | Default | Einheit | Status | Quelle | Bedeutung |", "|---|---|---|---|---|---|"]
    for r in parameter_rows(cfg):
        st = f"**{r['status']}**" if r["status"] == "Spez." else r["status"]
        cell = lambda v: str(v).replace("|", "\\|")
        out.append(f"| `{r['name']}` | {cell(_fmt(r['value']))} | {r['unit']} | {st} | {r['source']} | {cell(r['doc'])} |")
    out += ["", "## Standard-Messablauf (`blocks`)", "", "| id | kind | duration_s | random_rate | periodic_rate |",
            "|---|---|---|---|---|"]
    for b in (cfg or SimConfig()).blocks:
        out.append(f"| {b.id} | {b.kind} | {b.duration_s:g} | {b.random_rate:g} | {b.periodic_rate:g} |")
    return "\n".join(out) + "\n"
