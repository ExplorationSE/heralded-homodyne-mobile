# ExSE Harp ADQ32 HHD Mobil

Heralded-Homodyne-Detektion (HHD) mit einer PicoQuant **HydraHarp 500** (T2-Zeitmarken des Herald-Detektors) und
einem Teledyne SP Devices **ADQ32-PCIe** (Wellenformen des Homodyndetektors). Ziel von Release 1.0: Rekonstruktion
von Quadraturverteilung, Photonenzahlverteilung und Wigner-Funktion – phasengemittelt **und** mit bekannter Phase θ
(Entscheidung ED-11) – mit Anzeige auf dem Smartphone nach dem Muster von ExSE Harp GX Mobil.

**Stand 0.1.0.dev0:** Arbeitspaket AP2 **Simulator** (`hhd.sim`) und die Dateiformate (`hhd.io`). Import, Analyse,
Server und App folgen (AP3 ff., siehe Gesamtplan v1.3).

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
| `hhd/io/schema.py` | HDF5-Schema v1 „hhd-digitizer“ für ADQ32-Records (Daten, Kopfzeilen, Marker, Kalibrierung) mit Prüffunktion |
| `hhd/sim/config.py` | Konfiguration (YAML/JSON) als Parametertabelle mit Einheit, Status und Quelle; Grenzprüfung gegen die Gerätedaten |
| `hhd/sim/states.py` | Zustände: Vakuum, kohärent, thermisch, Fock \|1⟩ (geheraldet, mit η), Fock \|n⟩ und Katze als Erweiterung |
| `hhd/sim/phase.py` | Phase θ: zufällig (phasengemittelt), fest, Rampe, Stufen; Referenzmarker an die Harp |
| `hhd/sim/harp.py` | HydraHarp 500: 1 ps, Jitter, Totzeit, Kanal-Offsets, Marker-Eingänge mit Zeitfehler ≤ 100 ns |
| `hhd/sim/digitizer.py` | ADQ32: Records, TRIG-/SYNC-Zeitstempel, Pretrigger, Rearm, 12-bit-ADC, elektronisches Rauschen, Übersprechen |
| `hhd/sim/clock.py` | Uhrenmodell: gemeinsame 10-MHz-Referenz (ED-7) oder freie Uhr mit Frequenzfehler und Random Walk |
| `hhd/sim/engine.py` | Messablauf aus Blöcken (K-V, K-H, geheraldet, Zufallsfenster, getriggerte Zustände), Dateiausgabe, Ground Truth |
| `hhd/sim/check.py` | Referenzauswertung mit Ground Truth: Vakuumnormierung, Varianz, W(0,0), Phasenfit |
| `hhd/sim/stream.py` | Datenstrom-Modus: Wiedergabe in Chunks (optional Echtzeit), Hülle `StreamSource` als spätere Geräteschnittstelle |
| `hhd/sim/cli.py` | Kommandozeile `hhd-sim` |
| `config/*.yaml` | Vorlagen: Standard, bekannte Phase (kohärent), freie Uhr, Erweiterungszustände, Übersprechen |
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
hhd-sim run config/standard.yaml -o data/lauf1
hhd-sim validate data/lauf1                   # Schema v1, Prüfsummen, PTU-Köpfe
hhd-sim check data/lauf1                      # Sollwerte gegen Auswertung (mit Ground Truth)
hhd-sim stream config/standard.yaml --chunk 0.1 --speed 1   # Echtzeit-Wiedergabe
```

Ein Lauf schreibt:

| Datei | Inhalt |
|---|---|
| `harp_<block>.ptu` | HydraHarp-500-T2-Datei je Block (Herald, Zufallstrigger-Kennung, Marker) |
| `digitizer.h5` | ADQ32-Records aller Blöcke, Schema v1 |
| `truth.h5` | Ground Truth: wahre Zeiten, Moden, Quadraturen, Zuordnung Herald ↔ Record, Uhrphase |
| `run.json` | Manifest: Blöcke, Sollwerte, Konfiguration, SHA-256-Prüfsummen |

Beispiel `config/standard.yaml` (8 s Messzeit, Seed 20261020; Rechenzeit ≈ 5 s, 86 MB):

| Block | Art | Records | W(0,0) gemessen | Soll |
|---|---|---|---|---|
| `kv_start` | Vakuum (K-V) | 2000 | +0,315 ± 0,007 | +0,318 |
| `kh` | Herald blockiert (K-H) | 1006 | +0,309 ± 0,011 | +0,318 |
| `heralded` | geheraldet | 5103 | **−0,109 ± 0,008** | −0,105 |
| `heralded/random` | eingestreute Zufallsfenster | 1015 | +0,308 ± 0,011 | +0,318 |
| `random` | Zufallsfenster | 2033 | +0,318 ± 0,002 | +0,318 |
| `kv_end` | Vakuum (K-V) | 2000 | +0,317 ± 0,004 | +0,318 |

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
