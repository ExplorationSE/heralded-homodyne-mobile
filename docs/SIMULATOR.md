# Simulator `hhd.sim` – Modelle, Formate, Grenzen

Arbeitspaket AP2 des Gesamtplans v1.5. Der Simulator ist die Weiterentwicklung des Prototyps `sync_sim` aus dem
Vorbericht Synchronisation. Er erzeugt dieselben Dateien, die später die echten Geräte liefern:
**HydraHarp 500** (PTU, T2) und **ADQ32-PCIe** (HDF5-Schema v1). Zusätzlich schreibt er eine **Ground Truth**.
Damit lassen sich Import, Synchronisation, Kalibrierung und Rekonstruktion (AP3–AP7) ohne Hardware entwickeln
und prüfen.

Kennzeichnung im Text: **[Spez.]** = Herstellerunterlage, [Annahme] = Modellannahme oder Schätzwert, [Vorschlag] =
Festlegung dieses Projekts, [unsicher] = noch am Gerät zu prüfen. Alle Zahlenwerte mit Status und Quelle stehen in
[`PARAMETER.md`](PARAMETER.md).

## 1 Zeitachsen und Uhr

- Die Zeitbasis der Harp ist die **wahre Zeitachse**. Jede PTU-Datei (ein Block) beginnt bei 0 ps.
- Digitizer-Zeit: T_D(t) = (1 + ε)(t − t_arm) + φ(t). Dabei ist φ eine Irrfahrt mit ⟨Δφ²⟩ = D·τ.
- Standard ist ED-7, die gemeinsame 10-MHz-Referenz: ε = 0, φ = 0. Die Vorlage `config/free_clock.yaml` zeigt eine
  freie Uhr mit ε = 2 ppm und Irrfahrt.
- Sync-Marker laufen mit 1 kHz und pseudozufälligen Auslassungen (10 %) an beide Geräte:
  - an den Digitizer über SYNC;
  - an die Harp standardmäßig über den **Sync-Eingang** (in T2 ein vollwertiger Zeitstempelkanal mit 1 ps,
    Teiler „None“ **[Spez., Manual Kap. 6.3.2]**; Streuung 5 ps). Rückfall: Marker-Eingang M3 mit
    **[Spez.]** Zeitfehler < 100 ns (gaußförmig 30 ns, bei 100 ns abgeschnitten; `config/free_clock.yaml`).
- Der Zusammenhang Harp-Zeit → Digitizer-Zeit je Lauf steht als `mapping` in `session.json`.

## 2 Quelle und Zustände

- **SPDC-Paare (CW)**, Poisson-verteilt. Herald-Rate R_h, Nachweiswahrscheinlichkeit im Herald-Arm η_h.
  - Ungeheraldete Signalphotonen kommen mit der Rate R_h(1/η_h − 1).
  - Dunkelzählungen R_d erzeugen falsche Heralds, die Vakuum tragen.
- **Optik (Entscheidung 17.10.2026, Abschnitt `optics`):** Standard-Wellenlänge **1550 nm** (nur Metadatum:
  PTU-Kopf `UsrHHD_Wavelength_nm`, `UsrHHD_HeraldDetector`, Manifest `optics`, HDF5-Geräteattribute);
  Homodyn-Photodioden InGaAs-PIN mit `eta_hd_qe` = 0,95 [Annahme; Literatur > 98 % bei 1510–1575 nm,
  Opt. Express 2022, doi:10.1364/OE.447091]. Geprüft wird η ≤ eta_hd_qe ≤ 1.
- **Herald-Detektor als Preset** (`optics.herald_detector`); ein Preset setzt nur die Quellparameter, die nicht
  ausdrücklich angegeben sind, `custom` setzt keine:

  | Preset | η_det | R_d | Jitter (FWHM → σ) | Totzeit | Grundlage |
  |---|---|---|---|---|---|
  | `snspd` (Standard) | 0,85 | 100 /s | 40 ps → 17 ps | 30 ns | typische Herstellerwerte Single Quantum Eos, IDQ ID281 bei 1550 nm (konservativ gewählt) |
  | `ingaas_apd` | 0,20 | 3000 /s | 150 ps → 64 ps | 10 µs | ID Qube NIR Free-Running (20 %, Dunkelrate max. 3–6 kHz, Jitter typ. 150 ps, Totzeit 0,1–80 µs) |

  η_h = η_det · `herald_arm_T` (Kopplung und Filter im Herald-Arm, 0,6 [Annahme]); SNSPD: η_h = 0,51.
  Die **Totzeit** wird nicht-paralysierbar auf alle Herald-Ereignisse (echt und dunkel) angewandt; verlorene
  Heralds bleiben als ungeheraldete Signalphotonen im Signal. Warnung bei Totzeit·Rate > 5 %. **Nachpulsen**
  (InGaAs-APD) ist nicht modelliert. Wegen der Dunkelrate braucht die APD eine Herald-Rate ≫ 3 kHz
  (Vorlage `config/ingaas_apd.yaml` mit 30 kHz).
- **Temporale Mode:** f(t) = √γ·e^(−γ|t|) mit 1/γ = 40 ns, alternativ gaußförmig.
- **Verluste** werden exakt als Strahlteiler mit Vakuum modelliert: x = √η·x + √(1−η)·v.
- **Elektronisches Rauschen** (Clearance S) ist nach der Vakuumnormierung gleichwertig zu einem Verlust
  η_el = 1/(1 + 1/S).
- **Sollwerte** des geheraldeten Zustands:
  - W(0,0) = ξ·W₁(η·η_el) + (1 − ξ)/π, mit ξ = R_h/(R_h + R_d) und W₁(η) = (1 − 2η)/π;
  - Standardwerte η = 0,7, S = 15 dB: η_eff = 0,678, W₁ = −0,114; mit R_h = 1 kHz und R_d = 100 /s (SNSPD)
    ist ξ = 0,909 und W(0,0) = −0,074.
- **Zustände** (`hhd/sim/states.py`):
  - Vakuum, kohärent |α⟩, thermisch (n̄), Fock |1⟩;
  - als Erweiterungspunkt Fock |n⟩ und Katzenzustände |α⟩ ± |−α⟩.
  - Jeder Zustand liefert Stichproben x_θ und Sollwerte aus der Dichtematrix (⟨x⟩, Var x, W(0,0), ρ_nn).
  - Neue Zustände sind Unterklassen von `State` mit den Methoden `measure` und `rho`.
- **Phase θ** (ED-11, `hhd/sim/phase.py`):
  - `random`: phasengemittelt;
  - `fixed`;
  - `ramp`: Sägezahn mit f_ramp;
  - `steps`: Phasenstufen mit Verweildauer.
  - Den Beginn jeder Rampe bzw. Stufe meldet ein Marker an die Harp (Bitmaske 2), erstmals 1 µs nach Blockbeginn.
    Daraus rekonstruiert die Analyse θ je Record.

## 3 HydraHarp 500 (`hhd/sim/harp.py`)

**[Spez., HydraHarp 500 Manual]**:

- T2-Auflösung 1 ps; Zeitunsicherheit typ. 2,5 ps rms.
- Totzeit je Kanal < 680 ps (ETR) bzw. < 6,8 ns (CFD), modelliert als nicht verlängernd; Zusatz-Totzeit
  einstellbar.
- Eingänge: 4–8 beim Modell S, 8–16 beim Modell M, **zusätzlich** der Sync-Eingang; Eingangs-Offset ±100 ns.
- In T2 sind alle Eingänge einschließlich Sync gleichwertig; Sync-Ereignisse stehen als Special-Record mit
  Kanal 0 im Datenstrom (wie im Demo `tttrmode.py` des HH500Lib).
- 4 Marker-Eingänge (LVTTL am CTRL-Stecker, Pins 1–4) mit Hold-off; sie belegen **keinen** Eingangskanal,
  Marker stehen als Special-Record mit Bitmaske 1..15 im Datenstrom.

Kanalbelegung (Gesamtplan v1.5):

| Eingang | Signal |
|---|---|
| Sync (Kanal 0) | Sync-Marker (Pulsgenerator, 10-MHz-synchron, 1 kHz) |
| CH1 | Herald-Detektor |
| CH2 | Kopie der ADQ32-Triggerereignisse (Pulsgenerator am ADQ32-GPIO; Raster 6,4 ns) |
| CH3 | reserviert: zweiter Herald-Detektor bzw. PNR (nicht simuliert) |
| CH4 | Hilfstrigger: Zustandstrigger (Testzustände) bzw. Zufallstrigger-Kennung (Lauf `random`, Option AP20) |
| M1 | Laufmarke bei Laufbeginn und -ende (Quelle offen) |
| M2 | Phasenreferenz (Option θ) |
| M3 | Rückfall für Sync-Marker; sonst frei |
| M4 | frei |

Die Rollen stehen je Lauf im PTU-Kopf (`UsrHHD_ChannelRoles`, `UsrHHD_MarkerRoles`) und in den HDF5-Attributen.

Ausgabe: PTU **Generic T2** (0x00010207) mit Überlauf-Records (Wrap 2^25). Der Gerätename im Kopf lautet
„HydraHarp 500 S (SIMULATED)“. Dass das echte Gerät diesen Record-Typ schreibt, ist am Gerät noch zu prüfen
[unsicher].

Zusatz-Tags im Kopf:

- `UsrHHD_DatasetType`, `UsrHHD_BlockId`, `UsrHHD_RunName`, `UsrHHD_CalibrationId`;
- Phase und Kanalbelegung.

## 4 ADQ32-PCIe (`hhd/sim/digitizer.py`)

**[Spez., Datenblatt 20-2378 / User Guide 21-2539]**:

- Abtastung: 2,5 GS/s (S2G5) bzw. 2 GS/s (S2G0) je Kanal; im 1-Kanal-Modus doppelt so viel; 12 bit.
- Zeitauflösung: TRIG 50 ps und SYNC 3,2 ns bei 2,5 GS/s, linear skalierend.
- Trigger und Records:
  - TRIG höchstens 10 MHz; Rearm 20 ns;
  - Pretrigger in Schritten von 8 (2-Kanal) bzw. 16 (1-Kanal).
- Header: `timestamp` und `record_start` in `time_unit`; t_x[0] = timestamp + record_start.

Modellannahmen:

- `time_unit` = 1/(8·f_ADC); das ergibt 50 ps bei 2,5 GS/s und ist nicht dokumentiert.
- Der Record-Header meldet den auslösenden Eingang nicht (`trigger_source` = 255). Die Kennung, ob ein Record
  aus einem Herald oder einem Zufallstrigger stammt, kommt deshalb über Harp-CH4 (nur Option AP20).

Kanäle und Anschlüsse (Gesamtplan v1.5):

- **Kanal A:** Homodyndetektor. **Kanal B:** LO-Monitor-Photodiode, gleicher Trigger, eigener Sample Skip
  (Standard 25 → 100 MS/s, 200 Samples je Record) **[Spez.: Recordlänge, Offset, Skip je Kanal einstellbar]**;
  Modell: Dunkelwert + Signal ∝ P_LO, Intensitätsrauschen je Record, Rauschen je Sample [Annahme].
  **Pegel aus der Photodiodenkette** (Standard Thorlabs **PDA05CF2**, ≈ 1,0 A/W bei 1550 nm [Annahme, Peak
  1,04 A/W bei 1590 nm], 5 kV/A an 50 Ω, Rauschen 2 mV rms, **10-dB-Dämpfer**, Abgriff 150 µW, DC-Offset
  −0,2 V): Code = (V + Offset)/LSB mit LSB = 0,5 V/4096 → `lsb_nominal` 1943, `dark_lsb` −1625, `noise_lsb` 6,0.
  Die Prüfung meldet einen Fehler, wenn der Höchstpegel der Diode (5 V an 50 Ω) nach dem Dämpfer die
  **Abs. Max. ±1,75 V** überschreitet (also bei < 10 dB) oder |Offset| > 0,25 V.
  Gespeichert als `records/data_b`.
- **GPIO:** Ausgang eines Pulsgenerators mit der Triggerquelle des Laufs (TRIG-Port bzw. periodisch) → Harp CH2.
  Der Pulsgenerator gibt jedes Ereignis der Quelle aus, nicht nur angenommene Trigger **[Spez., UG Kap. 7.2]**.
- **TRIG:** Herald (Läufe A, K-H). **SYNC:** Sync-Marker. **CLK:** 10-MHz-Referenz (ED-7).

Signal am Analogeingang:

- Die Quadraturen der Moden werden mit der Modenfunktion als Wellenform eingeprägt.
- Dazu kommen Vakuumrauschen, elektronisches Rauschen, ADC-Offset und Quantisierung.
- Analogbandbreite (ED-14: **BW1G0**, 1 GHz) als Tiefpass 1. Ordnung mit −3 dB bei 1 GHz [Annahme; die
  Filterform ist nicht spezifiziert]. Er färbt das Rauschen je Sample (Varianz × 0,75 bei 2,5 GS/s), wirkt aber
  nicht auf die 40-ns-Mode; BW2G5 liegt über der Nyquist-Frequenz und filtert nicht.
- Optional wird Trigger-Übersprechen nachgebildet (Fehlerbild R10). Es erscheint am Modenort und wird von der
  Kontrolle K-H erkannt; siehe `config/crosstalk.yaml`.

Records sind 5000 Samples (2 µs) lang, davon 2000 Pretrigger. Sie enthalten damit **Off-Herald-Fenster**
vor und nach der Mode für die laufende Vakuumreferenz.

## 5 Messablauf: Sitzung aus getrennten Läufen (ED-12)

Ein Eintrag in `blocks` ist ein **Messlauf**: eigene PTU-Datei, eigene Gruppe in `digitizer.h5`, eigene Lauf-ID
(`<session.id>-R<nn>` oder `run_id`). Alle Läufe einer **Sitzung** teilen eine Kalibrierung (`calibration_id` =
`<Kampagne>-<Sitzung>-cal`); mehrere Sitzungen bilden eine **Kampagne** (`hhd-sim campaign`, `campaign.json`).
Jeder Lauf trägt seine Rolle im Vergleich (`comparison_role`: heralded, unheralded, control, test_state) und seine
Startzeit seit Sitzungsbeginn (`t_start_s`).

| kind | dataset_type | Trigger | Signal |
|---|---|---|---|
| `heralded` | heralded | Herald; optional eingestreut Zufallstrigger (`random_rate`) | alle SPDC-Moden |
| `random` | random_trigger | Zufallstrigger (Poisson) | alle SPDC-Moden (unbedingter Zustand) |
| `free_running` | free_running | periodisch intern (freilaufend) | alle SPDC-Moden (unbedingter Zustand) |
| `herald_blocked` | herald_blocked | Herald | Signal blockiert (Kontrolle K-H) |
| `vacuum` | vacuum | periodisch intern | Signal blockiert (Kontrolle K-V) |
| `electronic` | electronic | periodisch intern | LO aus |
| `state` | prepared_state | Zustandstrigger | Testzustand (kohärent, thermisch, Fock, Katze) |

Der **Standardablauf** (ab 0.5.0) ist der **M1-Laufplan** aus `config/laufplan_m1.yaml` (`M1_PLAN`:
K-V 20 s @ 10 kHz am Anfang, nach jedem 3. Zyklus und am Ende, K-H 60 s, 6 Zyklen A 60 s / B 30 s @ 2 kHz,
abschließender A; ab 0.8 mit Dunkellauf nach dem ersten K-V: 18 Läufe) im **Zeitraffer 1/90** (`SIM_TIME_SCALE`, ≈ 8 s Messzeit), damit `hhd-sim run`
schnell bleibt; die Vorlage selbst läuft in Echtzeit (12,1 min). Der Grundaufbau bleibt verschachtelt
K-V → K-H → A → B → A … → K-V; Lauf B
mit Zufallstrigger: `config/random_b.yaml`. Die
Heralds laufen auch im B-Lauf mit und werden in der PTU-Datei aufgezeichnet, triggern aber den Digitizer nicht.
Die frühere Variante mit eingestreuten Zufallstriggern ist als Option erhalten (`config/offherald_option.yaml`).

### Drift zwischen Läufen

`drift` simuliert, was getrennte Läufe gefährdet [Vorschlag, Gesamtplan v1.4 R15]:

- **LO-Leistung** P_LO(t) = lo_scale·(1 + a·t): optisches Signal und Vakuumrauschen skalieren mit √P_LO, das
  elektronische Rauschen nicht (die Clearance wird S·P_LO);
- **Effizienz** η(t) = η·eta_scale·(1 + b·t), je Lauf zur Laufmitte;
- **LO-Monitor**: Photodiode an ADQ32-Kanal B (Standard, `lo_monitor`); optional zusätzlich ein externer
  Leistungsmesser mit 10 Hz (`drift.monitor`, gespeichert unter `/blocks/<id>/monitor`).

Die Sollwerte je Lauf gelten für eine Normierung mit der Vakuumskala dieses Laufs.

**Stochastische Drift** (`hhd/sim/drift.py`, ab 0.4.0) [Annahme]: P_LO(t) und η(t) erhalten zusätzlich eine
Irrfahrt (`lo_rw_per_sqrt_s`, `eta_rw_per_sqrt_s`: Streuung der relativen Änderung nach τ ist q·√τ) und eine
Sinusschwankung mit zufälliger Phase (`*_sin_amp`, `*_sin_period_s`), auf einem Raster `grid_dt_s` linear
interpoliert; die Verstärkung des LO-Monitors driftet mit `lo_monitor.gain_rw_per_sqrt_s` (Teilerverhältnis,
Polarisation). Die Pfade stammen aus einem eigenen Zweig des Sitzungs-Seeds; Sitzungen ohne stochastische
Drift bleiben bitgleich. Die Wahrheit steht in `truth.h5` unter `/drift` (t_grid, lo, eta_factor, monitor_gain).
Szenarien „gut“, „typisch“, „schlecht“: `hhd.sim.drift.SCENARIOS`.

### Laufplan und Studie

`hhd/sim/plan.py` erzeugt Blockfolgen (`make_plan`: kv_00, kh_start, a_01, b_01, …, K-V nach je `kv_every`
Zyklen, a_n+1, kv_end) und berechnet Segmentzahlen, Datenvolumen, σ(η̂) = √((m4 − m2²)/N_A + 2·m2²/N_V)
mit m2 = ½ + η, m4 = ¾ + 3η (|1⟩ nach Verlust), σ(W(0,0)) = c_W/√N_A und den erwarteten Driftfehler je
Normierung exakt für Irrfahrt, lineare und periodische Drift (`drift_error`). `adapt_plan` ist die
Anpassungsregel nach der Stabilitätsmessung (AP14): Rauschmodell aus der Allan-Abweichung → größter
K-V-Abstand → T_A, Zyklen, K-V-Läufe. `hhd/sim/study.py` simuliert Sitzungen auf Quadraturebene
(Monte Carlo, ~0,2 s je Sitzung); `tests/test_plan_study.py` prüft Modell, schnelle Studie und volle Engine
gegeneinander. `tools/laufplan_studie.py` erzeugt die Abbildungen des Vorschlags M1.

Bei `--norm interpolated` enthält der ABA-Vergleich jetzt die Statistik der K-V-Läufe (verschiedene
Interpolationsgewichte); ohne diesen Term gab es rund 20 % Fehlalarme.

## 6 Dateien eines Laufs

| Datei | Inhalt |
|---|---|
| `harp_<lauf>.ptu` | T2-Records je Lauf; Kopf mit `UsrHHD_RunId`, `UsrHHD_SessionId`, `UsrHHD_CampaignId`, `UsrHHD_CalibrationId`, `UsrHHD_ComparisonRole` |
| `digitizer.h5` | Schema v1.2 (Kanal A und B), siehe Modulkopf von `hhd/io/schema.py` |
| `truth.h5` | Ground Truth je Block (s. u.) und Uhrphase `clock/` |
| `session.json` | Manifest mit Sitzung, Kalibrierung, Läufen, Sollwerten, `mapping`, Konfiguration, SHA-256 aller Dateien |

Gruppen der Ground Truth je Block in `truth.h5`:

| Gruppe | Inhalt |
|---|---|
| `heralds` | wahre Zeit, Harp-Zeit, zugehöriges Paar, Record, Dunkelzählung |
| `pairs` | Paare und ob sie geheraldet sind |
| `modes` | Modenzentrum (wahr und in Digitizer-Zeit), Art, θ, wahre Quadratur x |
| `records` | Art (0 Herald, 1 Zufall, 2 periodisch, 3 Zustand), Quelle, wahre Triggerzeit, Zahl überlappender Moden |
| `markers`, `random`, `state`, `phase_ref` | wahre Zeiten und Harp-Zeiten |

`hhd-sim validate` prüft:

- Schema v1.2, eine `calibration_id` je Sitzung, eindeutige Lauf-IDs;
- Prüfsummen;
- PTU-Köpfe: Record-Typ, Anzahl, Auflösung, Lauf-, Sitzungs- und Kalibrierungs-ID.

## 7 Datenstrom-Modus

`hhd.sim.stream.SimStream` spielt einen Lauf in Zeitscheiben ab, optional in Echtzeit oder beschleunigt. Ein
Chunk enthält:

- die rohen T2-Records, wie aus dem Harp-FIFO; `T2Decoder` dekodiert sie über Chunkgrenzen hinweg;
- die fertigen ADQ32-Records mit Header-Feldern;
- die SYNC-Marker.

`StreamSource` bietet `open`/`start`/`read_chunk`/`stop`/`status` wie die Quellen in GX. Darauf setzen später
die simulierten Geräte (AP17) und der Server auf. Die Daten sind bitgleich zum Datei-Modus.

**Speichersparender Datenstrom-Simulator** (`hhd.sim.streaming.StreamingSimulator`, ab 0.10): Er erzeugt jeden
Lauf in Zeitscheiben (`slice_s`) mit `simulate_block` und schreibt jede Scheibe sofort. Die PTU-Datei entsteht
im Datenstrom, `digitizer.h5` mit erweiterbaren Datensätzen, beides über `hhd.io.recorder`. Uhr-Irrfahrt und
stochastische Drift entstehen lazy in Segmenten. `truth.h5` enthält eine dezimierte Driftkurve und die
Sollwerte je Lauf.

Der Spitzenspeicher hängt nur von `slice_s` ab, nicht von der Laufdauer (Python-Speicher laut tracemalloc,
`slice_s` = 0,25 s, Recordlänge 1000):

| Laufdauer | Records | Spitzenspeicher | Daten Kanal A |
|---|---|---|---|
| 2 × 2 s | 6211 | 2,2 MB | 12 MB |
| 2 × 4 s | 12425 | 2,2 MB | 25 MB |
| 2 × 8 s | 24785 | 2,3 MB | 50 MB |

Bei `slice_s` = 1 s und Recordlänge 2000 liegt die Spitze bei etwa 300 MB, weil die Scheibe dann ganz im
Speicher liegt. Für lange Läufe deshalb 0,25 bis 0,5 s wählen.

Bewusste Unterschiede zum Datei-Modus:

* Die Zufallszahlen werden je Scheibe gezogen. Die Statistik ist gleich, die Dateien sind nicht bitgleich.
* Moden, die eine Scheibengrenze überlappen, fehlen (Anteil ≈ Recordlänge / `slice_s`).
* Laufmarken und Phasenmarker werden nicht erzeugt.
* `slice_s · f_marker` und `slice_s · periodic_rate` müssen ganzzahlig sein.

**Drift-Überwachung im Datenstrom** (`hhd.analysis.driftmon.DriftMonitor`): Sie hält nur laufende Summen,
der Speicher ist also konstant. Sie verfolgt:

* die LO-Leistung aus Kanal B (relativ zum ersten K-V, Dunkelwert abgezogen; Warnung > 1 %);
* die Vakuumvarianz je K-V (z-Test);
* η über die Überschussvarianz der A-Läufe (Var/Var_vac − ½, Var_vac mit dem LO-Monitor umgerechnet).

`trend()` legt gewichtete Geraden über die Läufe. Ergebnis in `tests/test_streaming_sim.py` (Sitzung 16,5 s,
4 A-Läufe):

| | LO-Trend | η-Trend |
|---|---|---|
| mit Drift: LO +2 %, η −35 % | +1,84 % (z = 52) | −36 % (z = −5,3) |
| ohne Drift (Kontrolle) | +0,002 % (z = 0,8) | z = −1,9 |

Die Warnung „LO-Leistung“ kommt nur bei Drift. Die η-Drift ist im Test groß gewählt, damit die kurze Sitzung
genug Statistik hat; die Empfindlichkeit wächst mit √(Records).

## 8 Referenzauswertung und Validierung

`hhd-sim check` (`hhd/sim/check.py`) wertet einen Lauf mit den **wahren** Modenpositionen aus. Die Schritte:

1. Vakuumnormierung aus den K-V-Blöcken;
2. Projektion auf die Modenfunktion;
3. EM-Schätzung von ρ_nn und daraus W(0,0) = Σ(−1)ⁿρ_nn/π, mit Bootstrap-Fehlern;
4. bei bekannter Phase ein Fit ⟨x⟩(θ) = A·cos(θ − φ).

Vakuumnormierung (`--norm`): `pooled` (gemeinsame Kalibrierung, Standard), `interpolated` (Vakuumvarianz
zeitlich linear zwischen den K-V-Läufen) oder `monitor` (gemeinsame Kalibrierung, je Lauf mit √P_LO aus dem
Monitor skaliert; vernachlässigt den elektronischen Anteil 1/S).

Der **Vergleich** fasst alle A-Läufe und alle B-Läufe zusammen (W(0,0), Var x, Differenz A − B mit z-Wert) und
meldet **Driftindikatoren** mit Warnung:

- A-B-A: erster gegen letzten A-Lauf (> 3σ);
- K-V: Vakuumvarianz erster gegen letzten K-V-Lauf (> 3σ);
- LO-Monitor: Änderung > 1 %.

Befund der Tests: Eine LO-Drift zeigen K-V und Monitor; `interpolated` und `monitor` korrigieren sie. Eine
Effizienzdrift sieht K-V nicht, nur A-B-A; bei linearer Drift ist das Mittel der A-Läufe trotzdem unverzerrt
gegenüber B.

Records eines Laufs werden nach Triggerart getrennt ausgewertet; `heralded/random` sind die eingestreuten
Zufallsfenster. Die Tests (`tests/test_validation.py`) prüfen u. a.:

- geheraldetes W(0,0) negativ und innerhalb 4σ vom Sollwert;
- K-V, K-H und Zufallsfenster mit Vakuum verträglich;
- Korrelation der rekonstruierten Quadratur je Record mit der wahren (> 0,98);
- kohärenter Zustand mit Rampe und Stufen: A und φ im Sollbereich;
- Fock |2⟩ und Katze im Sollbereich;
- Übersprechen wird über K-H erkannt.

## 9 Grenzen (Stand 0.5.0.dev0)

- Datenstrom-Simulator: Phasenmodi `ramp`/`steps` nicht unterstützt (siehe Abschnitt 7).
- Die Referenzauswertung braucht die Ground Truth. Die Zuordnung Herald ↔ Record allein aus Markern folgt in
  AP4 (Synchronisation), Kalibrierung und Rekonstruktion in AP5–AP7.
- Der Sollwert berücksichtigt keine überlappenden Nachbarmoden; bei R_h = 1 kHz und 1/γ = 40 ns ist das
  vernachlässigbar. `records/n_modes` zählt sie.
- Drift ist nur linear bzw. als Sprung je Lauf modelliert; keine LO-Phasen- oder Polarisationsdrift, kein
  Drift des Herald-Arms (η_h, Dunkelrate).
- Nicht modelliert sind:
  - Detektorbandbreite und -nachschwingen, LO-Intensitätsrauschen, 1/f-Rauschen;
  - FIFO-Überlauf bei hohen Raten;
  - Nachpulsen und Zeitabhängigkeit der Effizienz nach der Totzeit (InGaAs-APD), Wellenlängenabhängigkeit
    (die Wellenlänge ist nur Metadatum);
  - Architektur B (kontinuierliches Streaming), die nach ED-2b erst nach Release 1.0 kommt.
- Vom Gerät zu bestätigen [unsicher]:
  - `time_unit` des ADQ32;
  - ob der Record-Header den Trigger-Eingang meldet;
  - der PTU-Record-Typ der HydraHarp 500;
  - ob der ADQ32-Pulsgenerator nur angenommene Trigger ausgeben kann und seine Latenz;
  - Pegelanpassung GPIO (≥ 1,5 V an 50 Ω) → Harp-Eingang (Betriebsbereich ±1,2 V, optimal 150–300 mV).
