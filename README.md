# ExSE Harp ADQ32 HHD Mobil

Heralded-Homodyne-Detektion (HHD) mit einer PicoQuant **HydraHarp 500** (T2-Zeitmarken des Herald-Detektors) und
einem Teledyne SP Devices **ADQ32-PCIe** (Wellenformen des Homodyndetektors). Ziel von Release 1.0: Rekonstruktion
von Quadraturverteilung, Photonenzahlverteilung und Wigner-Funktion – phasengemittelt (Standard), mit bekannter
Phase θ als Option
(ED-11, optional) – mit Anzeige auf dem Smartphone nach dem Muster von ExSE Harp GX Mobil.

**Stand 0.10.0.dev0:** Arbeitspaket AP2 **Simulator** (`hhd.sim`), die Dateiformate (`hhd.io`), **Import**
(`hhd.io.session`), **Synchronisation/Zuordnung** (`hhd.sync`; `docs/SYNC.md`) sowie die **Auswertung**:
Normierung/Kalibrierung, Modenextraktion (parametrisch und PCA), phasengemittelte Rekonstruktion mit Bootstrap
und Vergleich A/B (`hhd.calib`, `hhd.modes`, `hhd.recon`, `hhd.analysis`; `docs/ANALYSE.md`), ab 0.8 mit
kampagnengeprüften Konfidenzintervallen (Abdeckung 68/95 %), Dunkellauf/η_el im Effizienzbudget, Nulltests (OP-5)
und kalibrierten Driftschwellen (OP-6). Ab 0.9 **Mess-Server und Web-App** (`hhd.server`; `docs/SERVER.md`):
Geräte HydraHarp 500/ADQ32 simuliert, als Wiedergabe oder live, Messablauf nach Laufplan,
Live-Vorschau, Auswertung per Auftrag, Ergebnisansicht auf dem Smartphone, Steuersperre, Protokoll, ED-8b.
Ab 0.10 **Treiber für die echten Geräte** (`hhd.drivers`; `docs/HARDWARE.md`): HH500Lib und ADQAPI über ctypes,
dynamisch geladen, gegen Mock-Bibliotheken getestet; **gemischter Betrieb** (je Gerät sim/replay/live, in jeder
Datei und in der App gekennzeichnet) und ein **speichersparender Datenstrom-Simulator** mit Drift-Überwachung.

**Messablauf (ED-12):** getrennte Messläufe einer **Sitzung** mit gemeinsamer Kalibrierung, verschachtelt
K-V → K-H → **A** (Herald-Trigger) → **B** (ohne Herald: freilaufender interner Trigger; Zufallstrigger als
Alternative `config/random_b.yaml`) → **A** → K-V.
Off-Herald-Fenster und eingestreute Zufallstrigger bleiben als Option (`config/offherald_option.yaml`).
**Geräte (Defaults):** HydraHarp 500 S mit Sync-Eingang + 4 Kanälen (Sync-Marker am Sync-Eingang, CH1 Herald,
CH2 ADQ32-Trigger-Kopie, CH3 reserviert, CH4 Hilfstrigger; M1 Laufmarke, M2 Phasenreferenz); ADQ32-PCIe S2G5
(2,5 GS/s), BW1G0 (1 GHz), Kanal A Homodyn, Kanal B LO-Monitor-Photodiode.
**Optik (Defaults, Entscheidung 17.10.2026):** 1550 nm (Metadatum), Herald-Detektor **SNSPD** (Preset `snspd`;
Alternative `ingaas_apd`, Vorlage `config/ingaas_apd.yaml`), Homodyn-Photodioden InGaAs (Quanteneffizienz 0,95),
LO-Monitor Thorlabs **PDA05CF2** mit 10-dB-Dämpfer an Kanal B. **Laufplan-Default:** M1-Plan aus
`config/laufplan_m1.yaml` (18 Läufe inkl. Dunkellauf) im Zeitraffer 1/90.

> [!IMPORTANT]
> **Hinweis – bitte zuerst lesen**
> - **Geräteanbindung ungeprüft.** Die Treiber für HydraHarp 500 und ADQ32 sind nach den öffentlichen Handbüchern
>   geschrieben und **nur gegen Mock-Bibliotheken** getestet, noch nicht an echter Hardware (Prüfliste in
>   `docs/HARDWARE.md`). Die Herstellerbibliotheken sind **nicht** enthalten und müssen vom Hersteller bezogen
>   werden. Die Betriebsart (sim, replay, live, gemischt) steht in jeder Datei; simulierte Dateien tragen
>   `simulated = true` bzw. „(SIMULATED)“ im Gerätenamen.
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
| `hhd/io/schema.py` | HDF5-Schema v1.2 „hhd-digitizer“ für ADQ32-Records (Kanal A, Kanal B = LO-Monitor, Kopfzeilen, Marker, Sitzungs-/Lauf-IDs, Kanalrollen, Kalibrierung) mit Prüffunktion |
| `hhd/sim/config.py` | Konfiguration (YAML/JSON) als Parametertabelle mit Einheit, Status und Quelle; Grenzprüfung gegen die Gerätedaten |
| `hhd/sim/states.py` | Zustände: Vakuum, kohärent, thermisch, Fock \|1⟩ (geheraldet, mit η), Fock \|n⟩ und Katze als Erweiterung |
| `hhd/sim/phase.py` | Phase θ: zufällig (phasengemittelt), fest, Rampe, Stufen; Referenzmarker an die Harp |
| `hhd/sim/harp.py` | HydraHarp 500: 1 ps, Jitter, Totzeit, Kanal-Offsets, Sync-Eingang als Zeitstempelkanal, Marker-Eingänge mit Zeitfehler ≤ 100 ns |
| `hhd/sim/digitizer.py` | ADQ32: Records, TRIG-/SYNC-Zeitstempel, Pretrigger, Rearm, 12-bit-ADC, Analogbandbreite, elektronisches Rauschen, LO-Drift, Übersprechen |
| `hhd/sim/clock.py` | Uhrenmodell: gemeinsame 10-MHz-Referenz (ED-7) oder freie Uhr mit Frequenzfehler und Random Walk |
| `hhd/sim/engine.py` | Sitzung aus getrennten Läufen (K-V, K-H, mit/ohne Herald, getriggerte Zustände), Drift, LO-Monitor, Kampagnen, Dateiausgabe, Ground Truth |
| `hhd/sim/check.py` | Referenzauswertung mit Ground Truth: Vakuumnormierung (gemeinsam / interpoliert / LO-Monitor), Varianz, W(0,0), Phasenfit, Vergleich A/B, Driftindikatoren |
| `hhd/sim/stream.py` | Datenstrom-Modus: Wiedergabe in Chunks (optional Echtzeit), Hülle `StreamSource` als spätere Geräteschnittstelle |
| `hhd/sim/drift.py` | Stochastische Drift (Irrfahrt, periodisch, linear) für LO, η und Monitor-Verstärkung; Allan-Abweichung und Rauschmodell-Fit; Driftszenarien [Annahme] |
| `hhd/sim/plan.py` | Laufplan M1: Blockfolge K-V/K-H/A/B, Segmentzahlen, Datenvolumen, σ(η), σ(W(0,0)), Driftfehler je Normierung (Modell), Anpassungsregel nach der Stabilitätsmessung (AP14) |
| `hhd/sim/study.py` | schnelle Sitzungssimulation auf Quadraturebene für Monte-Carlo-Studien (gegen die Engine getestet) |
| `hhd/sim/cli.py` | Kommandozeile `hhd-sim` |
| `hhd/io/session.py` | Import: Sitzung/Kampagne (Manifest-Whitelist, Prüfsummen, **ohne** `truth.h5`), PTU chunkweise, ADQ32-Records Kanal A/B blockweise |
| `hhd/sync/` | Synchronisation: `kernels` (Rust-Schnittstelle AP9), `markers` (Gitterindex, Kreuzkorrelation), `clockmap` (robust/stückweise linear), `assign` (Klassen, Totzeit, sekundär, Überlappung), `delay` (Δ über Varianzmaximum), `pipeline` (Bericht, `sync.h5`/`sync.json`), `stream` (Datenstrom) |
| `hhd/sim/sync_validate.py` | Validierung der Synchronisation gegen die Wahrheit (Fehlzuordnungen, Residuen, Pulls a, b, Δ) – nur Tests/`hhd validate` |
| `hhd/cli.py` | Kommandozeile `hhd` (import, sync, validate, stream, bench, calibrate, reconstruct, compare [--streaming], drift, report) |
| `hhd/drivers/` | Treiber (ctypes): `hh500.py` (HH500Lib, T2), `adq.py` (ADQAPI, JSON-Parameter, Record-Header 2.0), dynamisches Laden; `mock/` Mock-Bibliotheken in C für Tests |
| `hhd/io/recorder.py`, `stream_writer.py`, `modes.py` | Aufzeichnung im Datenstrom (PTU, `digitizer.h5`, `session.json`), Betriebsarten je Gerät |
| `hhd/sim/streaming.py` | speichersparender Datenstrom-Simulator (Zeitscheiben, lazy Drift, dezimierte Wahrheit) |
| `hhd/analysis/driftmon.py` | Drift-Überwachung im Datenstrom (LO, Vakuumvarianz, η) mit konstantem Speicher |
| `hhd/analysis/streaming.py` | Datenstrom-Auswertung langer Sitzungen (Histogramme, Momente, Bin-MaxLik, Poisson-Bootstrap) |
| `hhd/analysis/replaydrift.py` | Drift über eine Wiedergabe (`hhd drift`) |
| `hhd/io/chunked.py` | Lesen in Zeitfenstern (PTU, Digitizer-Köpfe, Marker) für Wiedergabe und lange Läufe |
| `hhd/server/` | Mess-Server (FastAPI): Geräteabstraktion live/sim/replay, Messablauf, Live-Vorschau/-Synchronisation, Auswertungsaufträge, Steuersperre, Befehlsprotokoll, Zugangscode, ED-8b; Web-App unter `hhd/server/web/` |
| `docs/SERVER.md` | Mess-Server und Web-App: Start, Bedienung, Rechte, Schnittstelle |
| `docs/HARDWARE.md` | echte Geräte: Bibliotheken installieren, Abbildung der Messung, Betriebsarten, Konfiguration, Inbetriebnahme-Prüfliste |
| `docs/SYNC.md` | Import und Synchronisation: Algorithmen, Klassen, Ausgabe, Validierung, Schnittstelle |
| `config/*.yaml` | Vorlagen: Standard (getrennte Läufe), freilaufender Trigger, Drift, Off-Herald-Option, bekannte Phase, freie Uhr, Erweiterungszustände, Übersprechen |
| `docs/SIMULATOR.md` | Modelle, Dateiformate, Grenzen, Quellen |
| `docs/PARAMETER.md` | vollständige Parametertabelle (erzeugt) |
| `tools/` | Read_PTU-kompatibler Leser, Neuerzeugung von Standardkonfiguration und Parametertabelle, Laufplan-Studie (`laufplan_studie.py`) |
| `tests/` | pytest-Suite |

## 2 Installation

Python 3.11–3.13.

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .                # stellt die Befehle hhd und hhd-sim bereit (alternativ: python -m hhd, python -m hhd.sim)
```

## 3 Benutzung

```bash
hhd-sim params                                # Parametertabelle (Default, Einheit, Status, Quelle)
hhd-sim config -o meine.yaml                  # vollständige Standardkonfiguration zum Anpassen
hhd-sim run config/standard.yaml -o data/S001         # eine Sitzung
hhd-sim campaign config/standard.yaml -o data/K1 -n 3 # Kampagne aus 3 Sitzungen (Seeds seed, seed+1, …)
hhd-sim validate data/S001                    # Schema v1.2, Prüfsummen, PTU-Köpfe, IDs
hhd-sim check data/S001                       # Sollwerte, Vergleich A/B, Driftindikatoren (mit Ground Truth)
hhd-sim check data/S001 --norm interpolated   # Vakuumnormierung zeitlich interpoliert (bzw. --norm monitor)
hhd-sim stream config/standard.yaml --chunk 0.1 --speed 1   # Echtzeit-Wiedergabe
hhd-sim plan --rate 1000 -o config/laufplan_m1.yaml   # Laufplan-Startwerte: Statistik, Datenvolumen, Blockfolge
hhd-sim plan --allan stabil.csv --rate 10000          # Anpassungsregel aus einer Stabilitätsmessung (Zeit s, Wert)
```

Struktur: **Kampagne** (`campaign.json`) → **Sitzung** (ein Verzeichnis, eine `calibration_id`) → **Läufe**
(je Lauf eine Lauf-ID `<Sitzung>-R<nn>`, eine PTU-Datei und eine Gruppe in `digitizer.h5`). Die IDs stehen in
Manifest, HDF5-Attributen und PTU-Kopf (`UsrHHD_RunId`, `UsrHHD_SessionId`, `UsrHHD_CampaignId`,
`UsrHHD_CalibrationId`). Eine Sitzung schreibt:

| Datei | Inhalt |
|---|---|
| `harp_<lauf>.ptu` | HydraHarp-500-T2-Datei je Lauf (Sync-Marker, Herald, Trigger-Kopie, Hilfstrigger, Laufmarke) |
| `digitizer.h5` | ADQ32-Records aller Läufe, Kanal A und B (LO-Monitor), Schema v1.2 |
| `truth.h5` | Ground Truth: wahre Zeiten, Moden, Quadraturen, Zuordnung Herald ↔ Record, Drift, Uhrphase |
| `session.json` | Manifest: Sitzung, Kalibrierung, Läufe, Sollwerte, Konfiguration, SHA-256-Prüfsummen |

Beispiel `config/standard.yaml` (M1-Laufplan im Zeitraffer: 18 Läufe inkl. Dunkellauf, 8,1 s Messzeit, Seed 20261020; Rechenzeit ≈ 8 s):

| Lauf | Art | Records | W(0,0) gemessen | Soll |
|---|---|---|---|---|
| `S001-R01` `kv_00` | Vakuum (K-V) | 2222 | +0,317 ± 0,006 | +0,318 |
| `S001-R02` `kh_start` | Herald, Signal blockiert (K-H) | 720 | +0,318 ± 0,005 | +0,318 |
| `S001-R03` `a_01` | A: Herald-Trigger | 709 | −0,058 ± 0,027 | −0,074 |
| `S001-R04` `b_01` | B: freilaufender Trigger | 665 | +0,318 ± 0,010 | +0,318 |
| … | a_02 … a_07, b_02 … b_06, kv_03 | | | |
| `S001-R17` `kv_end` | Vakuum (K-V) | 2221 | +0,315 ± 0,007 | +0,318 |
| A gesamt (7 Läufe) | | 5147 | **−0,072 ± 0,008** | −0,074 |
| B gesamt (6 Läufe) | | 3993 | +0,318 ± 0,002 | +0,318 |
| A − B | | | **−0,390 ± 0,008** (z = −46) | |

Der Sollwert −0,074 enthält η = 0,7, die Clearance 15 dB und den Anteil falscher Heralds (SNSPD-Dunkelrate
100 /s bei 1 kHz Herald-Rate, ξ = 0,909).
Driftindikatoren dieses Laufs: A-B-A z = +0,1 (W(0,0)), K-V-Varianz +0,4 % (z = +0,1), LO-Monitor (Kanal B) ±0,00 % – keine Warnung.
Mit `config/drift.yaml` zeigen K-V und LO-Monitor (Kanal B) die LO-Drift, die Effizienzdrift nur der A-B-A-Vergleich.

Die Auswertung in `hhd.sim.check` nutzt die **wahren** Modenpositionen aus `truth.h5`; sie prüft den Simulator,
nicht die spätere Analyse.

**Import und Synchronisation** (ohne Wahrheit; `docs/SYNC.md`):

```bash
hhd import data/S001             # Läufe, Records, Marker, Kanalrollen, Prüfsummen
hhd sync data/S001               # → data/S001/sync/sync.h5 + sync.json (Klassen, Uhrabbildung, Δ, Selbstprüfung)
hhd sync data/K1                 # Kampagne
hhd validate data/S001           # nur Simulation: Fehlzuordnungen, Pulls a, b, Δ gegen truth.h5
hhd stream data/S001             # chunkweiser Synchronisierer gegen Batch
hhd bench data/S001              # Durchsatz (Ereignisse/s)
```
(ohne Installation: `python -m hhd …`)

**Auswertung** (ohne Wahrheit; `docs/ANALYSE.md`):

```bash
hhd calibrate data/S001          # Quadraturen, Vakuum-/Elektronik-/LO-Kalibrierung, Drift, Übersprechen, PCA
hhd compare data/S001            # Rekonstruktion + Vergleich A/B → data/S001/analysis/result.json, report.md
hhd compare data/S001 --streaming   # dasselbe als Datenstrom (beschränkter Speicher, lange Sitzungen)
hhd drift data/S001              # Drift (LO, Vakuumvarianz, η) über eine Wiedergabe
hhd report data/S001             # Bericht ausgeben
hhd validate-analysis data/S001  # nur Simulation: Pulls gegen truth.h5
python tools/campaign_analysis.py data/K1 --n-boot 200 --procs 4 > studie.json   # Pulls/Bias/Abdeckung je KI-Methode
python tools/drift_thresholds.py null.json drift.json 0.0027 > op6.json         # Driftschwellen (OP-6)
```

**Mess-Server und Web-App** (`docs/SERVER.md`):

```bash
python -m hhd.server --config config/server.toml    # dann http://127.0.0.1:8766/ (LAN: http://<mess-pc>:8766/)
python -m hhd.server --mode replay --replay-dir data/S001   # Wiedergabe einer vorhandenen Sitzung
python -m hhd.server --config config/server_live.toml      # echte Geräte (docs/HARDWARE.md)
python -m hhd.server --harp-mode live --digitizer-mode sim  # gemischter Betrieb
tools/start_online.sh                                 # temporär online über cloudflared (Zugangscode Pflicht)
```

## 4 Tests

```bash
pytest -q
HHD_READ_PTU_PY=/pfad/zu/Read_PTU.py pytest tests/test_ptu.py   # optional: Vergleich mit dem offiziellen Read_PTU.py
```

Die Treiber- und Live-Tests bauen Mock-Bibliotheken mit dem vorhandenen C-Compiler (gcc/cc/clang); ohne Compiler
werden sie übersprungen. Der Vergleich mit dem PicoQuant-Demo `Read_PTU.py` wird übersprungen, wenn die Datei nicht angegeben ist; sie ist
nicht Teil des Repositorys.

## 5 Lizenz

MIT, siehe [LICENSE](LICENSE).
