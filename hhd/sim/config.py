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
class RunCfg:
    name: str = P("hhd_sim", doc="Name des Laufs (Dateinamen, Metadaten)", status="Vorschlag")
    seed: int = P(20261020, doc="Startwert des Zufallsgenerators; gleicher Seed = bitgleiche Dateien", status="Vorschlag")
    start_s: float = P(0.25, "s", doc="wahre Startzeit des ersten Blocks (Digitizer-Zähler läuft ab clock.t_arm_s)", status="Vorschlag")
    gap_s: float = P(0.5, "s", doc="Pause zwischen Blöcken (Umbau, z. B. Blocker)", status="Vorschlag")


@dataclass
class SourceCfg:
    R_herald: float = P(1000.0, "1/s", doc="Rate echter Heralds (CW, Poisson)", source=SYNC_REP)
    R_dark: float = P(20.0, "1/s", doc="Dunkelzählrate des Herald-Detektors (falsche Heralds, Vakuum)", source=SYNC_REP)
    eta_herald: float = P(0.5, "", doc="Nachweiswahrscheinlichkeit im Herald-Arm; bestimmt die Rate ungeheraldeter Signalphotonen R_herald·(1/η_h − 1)", source="Analyse Vergleich")
    eta: float = P(0.70, "", doc="Zustandseffizienz (Optik·Photodiode·Modenanpassung) vor elektronischem Rauschen", source=SYNC_REP)
    heralded_state: str = P("fock1", doc="Zustand der geheraldeten Mode (fock1; fock2 nur als Test)", status="Vorschlag")
    mode_shape: str = P("double_exponential", doc="Temporale Mode: double_exponential f(t)=√γ·e^(−γ|t|) (CW-SPDC) oder gauss", status="Vorschlag")
    gamma: float = P(1 / 40e-9, "1/s", doc="Abklingrate der Mode (1/γ = 40 ns); bei gauss: 1/σ", source=SYNC_REP)
    sigma_spd: float = P(50e-12, "s", doc="Jitter des Herald-Detektors (SNSPD), gemeinsam für Harp und TRIG", source=SYNC_REP)


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
    model: str = P("HydraHarp 500 M", doc="HydraHarp 500 S (4–8 Kanäle) oder M (8–16 Kanäle)", status="Spez.", source=HH500_MANUAL)
    n_channels: int = P(8, "", doc="Anzahl Eingangskanäle (S: 4..8, M: 8..16)", status="Spez.", source=HH500_MANUAL)
    resolution: float = P(1e-12, "s", doc="T2-Auflösung (fest 1 ps)", status="Spez.", source=HH500_MANUAL)
    jitter_rms: float = P(2.5e-12, "s", doc="Einzelmessungs-Zeitunsicherheit typ. (rms)", status="Spez.", source=HH500_MANUAL)
    trigger: str = P("ETR", doc="Eingangstrigger ETR (Totzeit < 680 ps) oder CFD (< 6,8 ns)", status="Spez.", source=HH500_MANUAL)
    tdead: float = P(0.0, "s", doc="programmierbare Zusatz-Totzeit (0 = native Totzeit)", status="Spez.", source="HydraHarp 500 Manual, Kap. 6.1")
    herald_channel: int = P(1, "", doc="Eingang des Herald-Detektors", status="Vorschlag")
    random_tag_channel: int = P(2, "", doc="Eingang mit der Kopie des Zufallstriggers (Kennung, Analyse Kap. 6)", status="Vorschlag")
    state_tag_channel: int = P(3, "", doc="Eingang mit dem Trigger getriggerter Testzustände", status="Vorschlag")
    marker_timing_channel: int = P(4, "", doc="Eingang für Sync-Marker, falls markers.on_timing_channel", status="Vorschlag")
    channel_offsets: dict = P({}, "s", doc="Eingangs-Offsets je Kanal {Kanal: s}, Bereich ±100 ns", status="Spez.", source=HH500_MANUAL)
    d_H: float = P(85.3e-9, "s", doc="Laufzeit Herald-Detektor → Harp", source=SYNC_REP)
    d_RH: float = P(10e-9, "s", doc="Laufzeit Zufalls-/Zustandstrigger-Generator → Harp", status="Annahme")
    marker_bit: int = P(1, "", doc="Marker-Bitmaske der Sync-Marker (Marker-Eingang 1)", status="Vorschlag")
    marker_error_rms: float = P(30e-9, "s", doc="Streuung des Marker-Zeitfehlers (Spez.: Fehler < 100 ns in T2; Gaußmodell = Annahme)", source=SYNC_REP)
    marker_offset: float = P(20e-9, "s", doc="konstante Marker-Latenz", source=SYNC_REP)
    marker_error_max: float = P(100e-9, "s", doc="Grenze des Marker-Zeitfehlers in T2 (Modell wird hier abgeschnitten)", status="Spez.", source=HH500_MANUAL)
    marker_holdoff: float = P(0.0, "s", doc="Marker-Hold-off (unterdrückt Folge-Marker)", status="Spez.", source="HydraHarp 500 Manual, Kap. 6.3")
    R_spurious: float = P(0.2, "1/s", doc="Störmarker (Glitches) an der Harp", source=SYNC_REP)


@dataclass
class DigitizerCfg:
    model: str = P("ADQ32", doc="Digitizer-Modell", status="Spez.", source="ED-6")
    form_factor: str = P("PCIe", doc="Bauform PCIe (ED-14) oder USB", status="Spez.", source=ADQ32_DS)
    clock_option: str = P("S2G5", doc="S2G5 (2,5 GS/s je Kanal, 5 GS/s 1-Kanal) oder S2G0 (2/4 GS/s)", status="Spez.", source=ADQ32_DS)
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
    on_timing_channel: bool = P(True, doc="Marker auf regulärem Timing-Kanal statt Marker-Eingang (ED-7)", status="Vorschlag", source="ED-7")
    sigma_tc: float = P(5e-12, "s", doc="Streuung, wenn der Marker auf einem Timing-Kanal liegt", source=SYNC_REP)
    d_MH: float = P(15e-9, "s", doc="Laufzeit Markerquelle → Harp", source=SYNC_REP)


@dataclass
class BlockCfg:
    id: str = P("heralded", doc="eindeutiger Blockname", status="Vorschlag")
    kind: str = P("heralded", doc="heralded | random | vacuum | herald_blocked | electronic | state", status="Vorschlag")
    duration_s: float = P(5.0, "s", doc="Messdauer des Blocks", status="Vorschlag")
    random_rate: float = P(0.0, "1/s", doc="Zufallstrigger (Poisson): eingestreut (heralded) bzw. Blocktrigger (random)", status="Vorschlag")
    periodic_rate: float = P(2000.0, "1/s", doc="interne periodische Trigger (vacuum, electronic)", status="Vorschlag")
    state: dict = P({}, doc="Testzustand bei kind=state: {type: coherent|thermal|fock|cat|vacuum|fock1, ...}", status="Vorschlag")
    state_rate: float = P(1000.0, "1/s", doc="Rate getriggerter Testzustände (periodisch)", status="Vorschlag")
    phase: dict = P({}, doc="blockeigene Überschreibung von phase.*", status="Vorschlag")
    crosstalk_amp: float | None = P(None, doc="blockeigene Überschreibung von digitizer.crosstalk_amp", status="Vorschlag")


def default_blocks() -> list[dict]:
    """Standard-Messablauf laut Gesamtplan (AP18) und Vergleichsanalyse: K-V, K-H, heralded (+ Zufall), Zufall, K-V."""
    return [
        dict(id="kv_start", kind="vacuum", duration_s=0.5, periodic_rate=4000.0),
        dict(id="kh", kind="herald_blocked", duration_s=1.0),
        dict(id="heralded", kind="heralded", duration_s=5.0, random_rate=200.0),
        dict(id="random", kind="random", duration_s=1.0, random_rate=2000.0),
        dict(id="kv_end", kind="vacuum", duration_s=0.5, periodic_rate=4000.0),
    ]


@dataclass
class SimConfig:
    run: RunCfg = field(default_factory=RunCfg)
    source: SourceCfg = field(default_factory=SourceCfg)
    phase: PhaseCfg = field(default_factory=PhaseCfg)
    harp: HarpCfg = field(default_factory=HarpCfg)
    digitizer: DigitizerCfg = field(default_factory=DigitizerCfg)
    clock: ClockCfg = field(default_factory=ClockCfg)
    markers: MarkerCfg = field(default_factory=MarkerCfg)
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
        chans = [h.herald_channel, h.random_tag_channel, h.state_tag_channel]
        if self.markers.on_timing_channel:
            chans.append(h.marker_timing_channel)
        if len(set(chans)) != len(chans):
            e.append("harp: Kanalbelegung doppelt")
        for c in chans:
            if not 1 <= c <= h.n_channels:
                e.append(f"harp: Kanal {c} außerhalb 1..{h.n_channels}")
        for c, off in h.channel_offsets.items():
            if abs(float(off)) > 100e-9:
                e.append(f"harp.channel_offsets[{c}]: Bereich ±100 ns")
        if h.marker_bit not in (1, 2, 4, 8) or (self.phase.marker_bit not in (0, 1, 2, 4, 8)):
            e.append("Marker-Bit: 1, 2, 4 oder 8 (vier Marker-Eingänge)")
        if self.phase.marker_bit and self.phase.marker_bit == h.marker_bit and not self.markers.on_timing_channel:
            e.append("phase.marker_bit und harp.marker_bit gleich")
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
        if d.adc_bits != 12:
            e.append("digitizer.adc_bits: ADQ32 hat 12 bit")
        if abs(self.clock.eps) > 3e-6 and not self.clock.shared_10MHz:
            w.append("clock.eps > ±3 ppm (ADQ32-Spezifikation der internen Referenz)")
        if d.form_factor == "USB":
            w.append("digitizer.form_factor=USB: max. 1,9 GByte/s; Architektur B mit voller Rate nicht möglich")
        if not 0 < s.eta <= 1 or not 0 < s.eta_herald <= 1:
            e.append("source.eta, eta_herald in (0, 1]")
        if s.heralded_state not in ("fock1", "fock2"):
            e.append("source.heralded_state: fock1 oder fock2")
        if s.mode_shape not in ("double_exponential", "gauss"):
            e.append("source.mode_shape: double_exponential oder gauss")
        ids = [b.id for b in self.blocks]
        if len(set(ids)) != len(ids):
            e.append("blocks: id doppelt")
        for b in self.blocks:
            if b.kind not in ("heralded", "random", "vacuum", "herald_blocked", "electronic", "state"):
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
        return cls(**kw)

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
    for gname in ("run", "source", "phase", "harp", "digitizer", "clock", "markers"):
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
