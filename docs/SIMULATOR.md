# Simulator `hhd.sim` – Modelle, Formate, Grenzen

Arbeitspaket AP2 des Gesamtplans v1.3. Der Simulator ist die Weiterentwicklung des Prototyps `sync_sim` aus dem
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
  - an die Harp über einen regulären Timing-Kanal (ED-7, Streuung 5 ps) oder über einen Marker-Eingang. Am
    Marker-Eingang gilt **[Spez.]** ein Zeitfehler < 100 ns; das Modell ist gaußförmig mit 30 ns und wird bei
    100 ns abgeschnitten.
- Der Zusammenhang Harp-Zeit → Digitizer-Zeit je Block steht als `mapping` in `run.json`.

## 2 Quelle und Zustände

- **SPDC-Paare (CW)**, Poisson-verteilt. Herald-Rate R_h, Nachweiswahrscheinlichkeit im Herald-Arm η_h.
  - Ungeheraldete Signalphotonen kommen mit der Rate R_h(1/η_h − 1).
  - Dunkelzählungen R_d erzeugen falsche Heralds, die Vakuum tragen.
- **Temporale Mode:** f(t) = √γ·e^(−γ|t|) mit 1/γ = 40 ns, alternativ gaußförmig.
- **Verluste** werden exakt als Strahlteiler mit Vakuum modelliert: x = √η·x + √(1−η)·v.
- **Elektronisches Rauschen** (Clearance S) ist nach der Vakuumnormierung gleichwertig zu einem Verlust
  η_el = 1/(1 + 1/S).
- **Sollwerte** des geheraldeten Zustands:
  - W(0,0) = ξ·W₁(η·η_el) + (1 − ξ)/π, mit ξ = R_h/(R_h + R_d) und W₁(η) = (1 − 2η)/π;
  - Standardwerte η = 0,7, S = 15 dB: η_eff = 0,665, W(0,0) = −0,105.
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
- Eingänge: 4–8 beim Modell S, 8–16 beim Modell M; Eingangs-Offset ±100 ns.
- 4 Marker-Eingänge mit Hold-off.

Kanalbelegung [Vorschlag]:

| Eingang | Signal |
|---|---|
| 1 | Herald-Detektor |
| 2 | Kopie des Zufallstriggers (Kennung der Zufallsfenster) |
| 3 | Trigger getriggerter Testzustände |
| 4 | Sync-Marker (ED-7) |

Ausgabe: PTU **Generic T2** (0x00010207) mit Überlauf-Records (Wrap 2^25). Der Gerätename im Kopf lautet
„HydraHarp 500 M (SIMULATED)“. Dass das echte Gerät diesen Record-Typ schreibt, ist am Gerät noch zu prüfen
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
  aus einem Herald oder einem Zufallstrigger stammt, kommt deshalb über Harp-Eingang 2.

Signal am Analogeingang:

- Die Quadraturen der Moden werden mit der Modenfunktion als Wellenform eingeprägt.
- Dazu kommen Vakuumrauschen, elektronisches Rauschen, ADC-Offset und Quantisierung.
- Optional wird Trigger-Übersprechen nachgebildet (Fehlerbild R10). Es erscheint am Modenort und wird von der
  Kontrolle K-H erkannt; siehe `config/crosstalk.yaml`.

Records sind 5000 Samples (2 µs) lang, davon 2000 Pretrigger. Sie enthalten damit **Off-Herald-Fenster**
vor und nach der Mode für die laufende Vakuumreferenz.

## 5 Messablauf (Blöcke)

| kind | dataset_type | Trigger | Signal |
|---|---|---|---|
| `heralded` | heralded | Herald, eingestreut Zufallstrigger | alle SPDC-Moden |
| `random` | random_trigger | Zufallstrigger (Poisson) | alle SPDC-Moden (unbedingter Zustand) |
| `herald_blocked` | herald_blocked | Herald | Signal blockiert (Kontrolle K-H) |
| `vacuum` | vacuum | periodisch intern | Signal blockiert (Kontrolle K-V) |
| `electronic` | electronic | periodisch intern | LO aus |
| `state` | prepared_state | Zustandstrigger | Testzustand (kohärent, thermisch, Fock, Katze) |

Der Standardablauf ist K-V → K-H → geheraldet → Zufallsfenster → K-V. Alle Blöcke eines Laufs tragen dieselbe
`calibration_id`, damit sie vergleichbar bleiben.

## 6 Dateien eines Laufs

| Datei | Inhalt |
|---|---|
| `harp_<block>.ptu` | T2-Records je Block |
| `digitizer.h5` | Schema v1, siehe Modulkopf von `hhd/io/schema.py` |
| `truth.h5` | Ground Truth je Block (s. u.) und Uhrphase `clock/` |
| `run.json` | Manifest mit Blöcken, Sollwerten, `mapping`, Konfiguration, SHA-256 aller Dateien |

Gruppen der Ground Truth je Block in `truth.h5`:

| Gruppe | Inhalt |
|---|---|
| `heralds` | wahre Zeit, Harp-Zeit, zugehöriges Paar, Record, Dunkelzählung |
| `pairs` | Paare und ob sie geheraldet sind |
| `modes` | Modenzentrum (wahr und in Digitizer-Zeit), Art, θ, wahre Quadratur x |
| `records` | Art (0 Herald, 1 Zufall, 2 periodisch, 3 Zustand), Quelle, wahre Triggerzeit, Zahl überlappender Moden |
| `markers`, `random`, `state`, `phase_ref` | wahre Zeiten und Harp-Zeiten |

`hhd-sim validate` prüft:

- Schema v1;
- Prüfsummen;
- PTU-Köpfe: Record-Typ, Anzahl, Auflösung.

## 7 Datenstrom-Modus

`hhd.sim.stream.SimStream` spielt einen Lauf in Zeitscheiben ab, optional in Echtzeit oder beschleunigt. Ein
Chunk enthält:

- die rohen T2-Records, wie aus dem Harp-FIFO; `T2Decoder` dekodiert sie über Chunkgrenzen hinweg;
- die fertigen ADQ32-Records mit Header-Feldern;
- die SYNC-Marker.

`StreamSource` bietet `open`/`start`/`read_chunk`/`stop`/`status` wie die Quellen in GX. Darauf setzen später
die simulierten Geräte (AP17) und der Server auf. Die Daten sind bitgleich zum Datei-Modus.

## 8 Referenzauswertung und Validierung

`hhd-sim check` (`hhd/sim/check.py`) wertet einen Lauf mit den **wahren** Modenpositionen aus. Die Schritte:

1. Vakuumnormierung aus den K-V-Blöcken;
2. Projektion auf die Modenfunktion;
3. EM-Schätzung von ρ_nn und daraus W(0,0) = Σ(−1)ⁿρ_nn/π, mit Bootstrap-Fehlern;
4. bei bekannter Phase ein Fit ⟨x⟩(θ) = A·cos(θ − φ).

Records eines Blocks werden nach Triggerart getrennt ausgewertet; `heralded/random` sind die eingestreuten
Zufallsfenster. Die Tests (`tests/test_validation.py`) prüfen u. a.:

- geheraldetes W(0,0) negativ und innerhalb 4σ vom Sollwert;
- K-V, K-H und Zufallsfenster mit Vakuum verträglich;
- Korrelation der rekonstruierten Quadratur je Record mit der wahren (> 0,98);
- kohärenter Zustand mit Rampe und Stufen: A und φ im Sollbereich;
- Fock |2⟩ und Katze im Sollbereich;
- Übersprechen wird über K-H erkannt.

## 9 Grenzen (Stand 0.1.0.dev0)

- Die Referenzauswertung braucht die Ground Truth. Die Zuordnung Herald ↔ Record allein aus Markern folgt in
  AP4 (Synchronisation), Kalibrierung und Rekonstruktion in AP5–AP7.
- Der Sollwert berücksichtigt keine überlappenden Nachbarmoden; bei R_h = 1 kHz und 1/γ = 40 ns ist das
  vernachlässigbar. `records/n_modes` zählt sie.
- Nicht modelliert sind:
  - Detektorbandbreite und -nachschwingen, LO-Intensitätsrauschen, 1/f-Rauschen;
  - FIFO-Überlauf bei hohen Raten;
  - Architektur B (kontinuierliches Streaming), die nach ED-2b erst nach Release 1.0 kommt.
- Vom Gerät zu bestätigen [unsicher]:
  - `time_unit` des ADQ32;
  - ob der Record-Header den Trigger-Eingang meldet;
  - der PTU-Record-Typ der HydraHarp 500.
