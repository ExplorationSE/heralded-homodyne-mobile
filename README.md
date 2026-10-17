# ExSE Harp ADQ32 HHD Mobil

Heralded-Homodyne-Detektion (HHD) mit einer PicoQuant **HydraHarp 500** (T2-Zeitmarken des Herald-Detektors) und
einem Teledyne SP Devices **ADQ32-PCIe** (Wellenformen des Homodyndetektors). Ziel von Release 1.0: Rekonstruktion
von Quadraturverteilung, Photonenzahlverteilung und Wigner-Funktion – phasengemittelt (Standard), mit bekannter
Phase θ als Option
(ED-11, optional) – mit Anzeige auf dem Smartphone nach dem Muster von ExSE Harp GX Mobil.

**Stand 0.2.0.dev0:** Arbeitspaket AP2 **Simulator** (`hhd.sim`) und die Dateiformate (`hhd.io`). Import, Analyse,
Server und App folgen (AP3 ff., siehe Gesamtplan v1.4).

**Messablauf (ED-12):** getrennte Messläufe einer **Sitzung** mit gemeinsamer Kalibrierung, verschachtelt
K-V → K-H → **A** (Herald-Trigger) → **B** (ohne Herald: Zufalls- oder freilaufender Trigger) → **A** → K-V.
Off-Herald-Fenster und eingestreute Zufallstrigger bleiben als Option (`config/offherald_option.yaml`).
**Geräte (Defaults):** HydraHarp 500 S mit 4 Kanälen; ADQ32-PCIe S2G5 (2,5 GS/s), BW1G0 (1 GHz).

> [!IMPORTANT]
> **Hinweis – bitte zuerst lesen**
> - **Keine Geräteanbindung.** Dieser Stand erzeugt ausschließlich **simulierte Daten**. Alle Dateien tragen die
>   Kennung `simulated = true` bzw. „(SIMULATED)“ im Gerätenamen.
> - **Erstellt ausschließlich aus öffentlich verfügbaren Informationen:** HydraHarp-500-Handbuch, öffentliche
>   PTU-Formatbeschreibung und Demos von PicoQuant, Datenblatt (20-2378) und Benutzerhandbuch (21-2539) des ADQ32.
>   Das Repository enthält **keine Dateien von PicoQuant oder Teledyne** (keine DLLs, Header, Demo-Code, Treiber).
>   Der Leser in `tools/read_ptu_compat.py` ist eine eigene Implementierung mit dem Ausgabeformat von Read_PTU.
> - **Kein Produkt der PicoQuant GmbH oder von Teledyne SP Devices**, nicht mit ihnen verbunden, nicht von ihnen
>   geprüft. HydraHarp, PicoQuant, ADQ32 und Teledyne sind Marken ihrer jeweiligen Inhaber.
> - **Ohne Gewährleistung** (MIT-Lizenz); nicht für sicherheitskritische Anwendungen bestimmt.

## 1 Inhalt

| Pfad | Inhalt |
|---|---|
| `hhd/io/ptu.py` | PTU-Format **Generic T2** (0x00010207): Kodierer, zustandsbehafteter Dekoder (auch für Datenstrom-Chunks), Kopf mit `UsrHHD_*`-Metadaten |
| `hhd/io/schema.py` | HDF5-Schema v1.1 „hhd-digitizer“ für ADQ32-Records (Daten, Kopfzeilen, Marker, LO-Monitor, Sitzungs-/Lauf-IDs, Kalibrierung) mit Prüffunktion |
| `hhd/sim/config.py` | Konfiguration (YAML/JSON) als Parametertabelle mit Einheit, Status und Quelle; Grenzprüfung gegen die Gerätedaten |
| `hhd/sim/states.py` | Zustände: Vakuum, kohärent, thermisch, Fock \|1⟩ (geheraldet, mit η), Fock \|n⟩ und Katze als Erweiterung |
| `hhd/sim/phase.py` | Phase θ: zufällig (phasengemittelt), fest, Rampe, Stufen; Referenzmarker an die Harp |
| `hhd/sim/harp.py` | HydraHarp 500: 1 ps, Jitter, Totzeit, Kanal-Offsets, Marker-Eingänge mit Zeitfehler ≤ 100 ns |
| `hhd/sim/digitizer.py` | ADQ32: Records, TRIG-/SYNC-Zeitstempel, Pretrigger, Rearm, 12-bit-ADC, Analogbandbreite, elektronisches Rauschen, LO-Drift, Übersprechen |
| `hhd/sim/clock.py` | Uhrenmodell: gemeinsame 10-MHz-Referenz (ED-7) oder freie Uhr mit Frequenzfehler und Random Walk |
| `hhd/sim/engine.py` | Sitzung aus getrennten Läufen (K-V, K-H, mit/ohne Herald, getriggerte Zustände), Drift, LO-Monitor, Kampagnen, Dateiausgabe, Ground Truth |
| `hhd/sim/check.py` | Referenzauswertung mit Ground Truth: Vakuumnormierung (gemeinsam / interpoliert / LO-Monitor), Varianz, W(0,0), Phasenfit, Vergleich A/B, Driftindikatoren |
| `hhd/sim/stream.py` | Datenstrom-Modus: Wiedergabe in Chunks (optional Echtzeit), Hülle `StreamSource` als spätere Geräteschnittstelle |
| `hhd/sim/cli.py` | Kommandozeile `hhd-sim` |
| `config/*.yaml` | Vorlagen: Standard (getrennte Läufe), freilaufender Trigger, Drift, Off-Herald-Option, bekannte Phase, freie Uhr, Erweiterungszustände, Übersprechen |
| `docs/SIMULATOR.md` | Modelle, Dateiformate, Grenzen, Quellen |
| `docs/PARAMETER.md` | vollständige Parametertabelle (erzeugt) |
| `tools/` | Read_PTU-kompatibler Leser, Neuerzeugung von Standardkonfiguration und Parametertabelle |
| `tests/` | pytest-Suite |

## 2 Installation

Python 3.11–3.13.

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .                # stellt den Befehl hhd-sim bereit (alternativ: python -m hhd.sim)
```

## 3 Benutzung

```bash
hhd-sim params                                # Parametertabelle (Default, Einheit, Status, Quelle)
hhd-sim config -o meine.yaml                  # vollständige Standardkonfiguration zum Anpassen
hhd-sim run config/standard.yaml -o data/S001         # eine Sitzung
hhd-sim campaign config/standard.yaml -o data/K1 -n 3 # Kampagne aus 3 Sitzungen (Seeds seed, seed+1, …)
hhd-sim validate data/S001                    # Schema v1.1, Prüfsummen, PTU-Köpfe, IDs
hhd-sim check data/S001                       # Sollwerte, Vergleich A/B, Driftindikatoren (mit Ground Truth)
hhd-sim check data/S001 --norm interpolated   # Vakuumnormierung zeitlich interpoliert (bzw. --norm monitor)
hhd-sim stream config/standard.yaml --chunk 0.1 --speed 1   # Echtzeit-Wiedergabe
```

Struktur: **Kampagne** (`campaign.json`) → **Sitzung** (ein Verzeichnis, eine `calibration_id`) → **Läufe**
(je Lauf eine Lauf-ID `<Sitzung>-R<nn>`, eine PTU-Datei und eine Gruppe in `digitizer.h5`). Die IDs stehen in
Manifest, HDF5-Attributen und PTU-Kopf (`UsrHHD_RunId`, `UsrHHD_SessionId`, `UsrHHD_CampaignId`,
`UsrHHD_CalibrationId`). Eine Sitzung schreibt:

| Datei | Inhalt |
|---|---|
| `harp_<lauf>.ptu` | HydraHarp-500-T2-Datei je Lauf (Herald, Zufallstrigger-Kennung, Marker) |
| `digitizer.h5` | ADQ32-Records aller Läufe, LO-Monitor, Schema v1.1 |
| `truth.h5` | Ground Truth: wahre Zeiten, Moden, Quadraturen, Zuordnung Herald ↔ Record, Drift, Uhrphase |
| `session.json` | Manifest: Sitzung, Kalibrierung, Läufe, Sollwerte, Konfiguration, SHA-256-Prüfsummen |

Beispiel `config/standard.yaml` (8 s Messzeit in 6 Läufen, Seed 20261020; Rechenzeit ≈ 5 s):

| Lauf | Art | Records | W(0,0) gemessen | Soll |
|---|---|---|---|---|
| `S001-R01` `kv_start` | Vakuum (K-V) | 2000 | +0,315 ± 0,007 | +0,318 |
| `S001-R02` `kh` | Herald, Signal blockiert (K-H) | 1006 | +0,310 ± 0,009 | +0,318 |
| `S001-R03` `heralded_1` | A: Herald-Trigger | 2520 | −0,112 ± 0,013 | −0,105 |
| `S001-R04` `unheralded` | B: Zufallstrigger | 2033 | +0,318 ± 0,002 | +0,318 |
| `S001-R05` `heralded_2` | A: Herald-Trigger | 2533 | −0,091 ± 0,011 | −0,105 |
| `S001-R06` `kv_end` | Vakuum (K-V) | 2000 | +0,318 ± 0,004 | +0,318 |
| A gesamt | | 5053 | **−0,102 ± 0,008** | −0,105 |
| A − B | | | **−0,420 ± 0,008** (z = −51) | |

Driftindikatoren dieses Laufs: A-B-A z = +1,2 (W(0,0)), K-V-Varianz −2,5 % (z = −0,6), LO-Monitor +0,07 % – keine Warnung.
Mit `config/drift.yaml` zeigen K-V und LO-Monitor die LO-Drift, die Effizienzdrift nur der A-B-A-Vergleich.

Die Auswertung in `hhd.sim.check` nutzt die **wahren** Modenpositionen aus `truth.h5`; sie prüft den Simulator,
nicht die spätere Analyse (die muss Herald und Record allein aus Markern zuordnen, AP4/AP5).

## 4 Tests

```bash
pytest -q
HHD_READ_PTU_PY=/pfad/zu/Read_PTU.py pytest tests/test_ptu.py   # optional: Vergleich mit dem offiziellen Read_PTU.py
```

Der Vergleich mit dem PicoQuant-Demo `Read_PTU.py` wird übersprungen, wenn die Datei nicht angegeben ist; sie ist
nicht Teil des Repositorys.

## 5 Lizenz

MIT, siehe [LICENSE](LICENSE).
