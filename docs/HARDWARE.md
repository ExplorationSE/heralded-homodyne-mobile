# Echte Geräte: HydraHarp 500 S und ADQ32 (`hhd.drivers`, `hhd.server.livedev`)

Stand 0.10.0.dev0. Die Treiber sind schlanke ctypes-Anbindungen an die Herstellerbibliotheken. Sie sind
**nur nach öffentlichen Handbüchern** geschrieben und **noch nicht an echter Hardware erprobt**. Getestet sind
sie gegen Mock-Bibliotheken mit derselben C-Schnittstelle (`hhd/drivers/mock/`). Alle Stellen, die am Gerät zu
prüfen sind, tragen im Code die Markierung **[unsicher]** und stehen gesammelt in Abschnitt 6. Später können die
Anbindungen durch den Rust-Kern ersetzt werden (ED-4); die Schnittstelle `Device` bleibt dabei gleich.

| Modul | Inhalt |
|---|---|
| `hhd/drivers/_loader.py` | dynamisches Laden (`ctypes.CDLL`), Suchreihenfolge, `LibraryNotFound` mit Installationshinweis |
| `hhd/drivers/hh500.py` | HH500Lib: Gerät öffnen, T2 initialisieren, Eingänge/CFD/Pegel/Offsets/Sync-Teiler/Marker, Messung mit CTC, FIFO-Auslese (`TTREADMAX`), Flags/Warnungen, Debug-Dump, T2-Decoder |
| `hhd/drivers/adq.py` | ADQAPI: Versionsprüfung, Steuereinheit, Gerätesuche, JSON-Parameter (`get/set/update`), `configure_homodyne`, Start/Stopp, `WaitForRecordBuffer`/`ReturnRecordBuffer`, `ADQGen4RecordHeader` 2.0 (72 Byte) |
| `hhd/server/livedev.py` | Live-Geräte `HydraHarp500Live`, `ADQ32Live` hinter der Server-Abstraktion (Auslese-Thread je Gerät, Laufzeitfenster, Fehler als `DeviceError`) |
| `hhd/io/recorder.py`, `hhd/io/stream_writer.py` | Aufzeichnung im Datenstrom: PTU je Lauf, `digitizer.h5` (Schema 1.2), `session.json` |
| `hhd/io/modes.py` | Betriebsart je Gerät (sim, replay, live) und Sitzung (sim, replay, live, mixed) |

## 1 Herstellerbibliotheken installieren

Beide Bibliotheken werden **erst beim Öffnen des Geräts** geladen. Fehlen sie, bleibt der Server lauffähig.
Er meldet dann klar, welche Bibliothek fehlt und wie man sie installiert, und verweigert den Start
(`live_fallback = "refuse"`, Standard). Alternativ fällt er auf die Simulation zurück (`live_fallback = "sim"`).
Der Rückfall wird deutlich angezeigt und in den Dateien vermerkt.

Suchreihenfolge je Bibliothek: Konfigurationseintrag (`hh500_lib`, `adq_lib`), dann die Umgebungsvariable
(`HHD_HH500LIB`, `HHD_ADQAPI`), dann die Standardorte.

**HydraHarp 500 – HH500Lib (PicoQuant)**

* Linux: Paket von PicoQuant entpacken und `sudo ./install` ausführen. Das legt
  `/opt/picoquant/hh500lib/libhh500lib.so` ab und installiert die udev-Regel `HydraHarp500.rules`.
  Anschließend neu einstecken bzw. `udevadm control --reload`.
  **[unsicher]:** Die USB-Produkt-ID in den Unterlagen ist nicht eindeutig (0013 bzw. 0016). Am Gerät mit `lsusb`
  prüfen und gegebenenfalls die udev-Regel anpassen.
* Windows: `setup.exe` von PicoQuant installiert `HH500Lib.dll`.
* Prüfen: `python -c "from hhd.drivers.hh500 import HH500Lib; print(HH500Lib().library_version())"`.
  Für das ADQ32: `python -c "from hhd.drivers.adq import ADQAPILib; print(ADQAPILib().validate_version())"`
  (0 = kompatibel).

**ADQ32 – ADQAPI (Teledyne SP Devices)**

* Linux: `TSPD_SDK_linux_<version>.tar.gz` laut README installieren. Es liefert `libadq.so` und den
  PCIe-Kerneltreiber.
* Windows: Installer des SDK; er liefert `ADQAPI.dll`.
* Der Treiber prüft die Version mit `ADQAPI_ValidateVersion(15, 0)`. Ist die Bibliothek inkompatibel, bricht er
  mit einer Meldung ab. Ist sie nur rückwärtskompatibel, gibt er eine Warnung aus.
* Fehlerprotokoll der Bibliothek: `adq_trace_dir` (Standard `logs/adq_trace`).

Die Software stellt **keine Anfrage an den Hersteller**. Offene Punkte werden am Gerät geklärt (Abschnitt 6).

**Mock-Bibliotheken** (ohne Hardware, für Tests und Vorführung):

```bash
python -c "from hhd.drivers.mock.build import build; print(build('/tmp/hhdmock'))"   # braucht gcc/cc/clang
export HHD_HH500LIB=/tmp/hhdmock/libhh500mock.so HHD_ADQAPI=/tmp/hhdmock/libadqmock.so
python -m hhd.server --mode live            # „echter“ Live-Pfad gegen die Mocks
```

Die Mocks erzeugen konsistente Herald-, Marker- und LO-Signale. Mit Umgebungsvariablen lassen sich Fehler
erzwingen:

* `MOCK_HH500_NODEV`, `MOCK_HH500_SYSERROR`, `MOCK_HH500_FIFOFULL_MS`, `MOCK_HERALD_RATE`;
* `MOCK_ADQ_NODEV`, `MOCK_ADQ_OVERFLOW_AFTER`, `MOCK_ADQ_VERSION`.

## 2 Abbildung der Messung

**HydraHarp 500 S (T2):**

* `Initialize(devidx, mode=2, refsource)` mit Referenz 0 = intern bzw. 1 = 10 MHz extern (ED-7).
* Kanal 0 ist der Sync-Eingang und trägt die SYNC-Marker (1 ps). Danach folgen die Eingänge nach
  `harp_channel_roles`: Standard CH1 = Herald, CH2 = Triggerkopie des ADQ32.
* Eingeschaltet werden nur Eingänge mit einer Rolle. Der Sync-Teiler ist im Live-Betrieb fest 1, weil T2 jedes
  Sync-Ereignis als Marker braucht.
* Je Lauf werden Triggerart (ETR/CFD), Flanke, Pegel und Offsets gesetzt, solange keine Messung läuft
  (Set-Aufrufe sind während einer Messung nicht erlaubt).
* Messung: `SetMeasControl(0)` (CTC), dann `StartMeas(T)` und `ReadFiFo` in einer Schleife, bis `CTCStatus`
  das Ende meldet. Danach wird der Rest ausgelesen.
* `FIFOFULL` oder `SYSERROR` beenden den Lauf mit Fehler und schreiben einen Debug-Dump nach `log_dir`.
* Die Records werden **unverändert** in die PTU geschrieben. Sie haben das HydraHarp-T2-Format: 1 Spezial-Bit,
  6 Bit Kanal, 25 Bit Zeit; Kanal 63 markiert einen Überlauf.

**ADQ32 (PCIe):**

* Gerät: `CreateADQControlUnit`, `ListDevices`, `SetupDevice(adq_device_index)`, Parameter als JSON. Der
  Treiber liest stets zuerst die Geräte-JSON und ändert nur bekannte Schlüssel; ein unbekannter Schlüssel ist
  ein Fehler.
* **Kanal A (Homodyn):** `input_range_mV`, `dc_offset_mV`, Recordlänge 5000, Pretrigger 2000
  (`horizontal_offset = −2000`). Trigger je Lauf: TRIG (Herald) oder PERIODIC (freilaufend, Vakuum).
* **Kanal B (LO-Monitor, PDA05CF2 + 10-dB-Dämpfer):** Sample Skip 25 (100 MS/s), eigener Bereich und
  DC-Offset (unipolar).
* **Marker (ED-7), `adq_marker_capture`:**
  * `channel_b_sync` (Standard): Kanal B wird vom **SYNC-Port** getriggert. Die Header der B-Records liefern die
    Markerzeiten, ihre Daten den LO-Monitor zum Markerzeitpunkt. In `digitizer.h5` landet das unter
    `markers/timestamp` und `markers/data_b`; das Geräteattribut ist `channel_b_trigger = "SYNC"`.
    `records/data_b` entfällt. Die Analyse interpoliert den LO-Monitor an den Triggerzeiten
    (`record_means_b`).
  * `channel_b_trig`: Kanal B mit dem Trigger von Kanal A, also gepaarte Records `records/data_b`. Dann gibt es
    keine Markerzeiten am ADQ32 (nur für Prüfungen ohne Synchronisation).
* Die Zeitbasis kommt aus dem Header (`time_unit`, `sampling_period`) und wird mit dem Nennwert verglichen.
  Die Triggerquelle meldet der Header nicht; deshalb steht `trigger_source = 255` mit
  `header_reports_trigger_source = False`.
* Lauf: Start, dann `WaitForRecordBuffer`/`ReturnRecordBuffer` in einem eigenen Thread.
  * Behalten werden die Records im Gerätezeitfenster [erster Record, + T]; danach wird gestoppt. Zur Sicherheit
    begrenzt eine Host-Zeitgrenze den Lauf auf T + `adq_drain_ms` + 1 s.
  * `ADQ_EOVERFLOW` oder Statusbits für verlorene Daten beenden den Lauf mit Fehler.
* **Triggerkopie an HydraHarp CH2** (Pulsgenerator → GPIO): Der Treiber setzt sie **nicht** selbst
  **[unsicher]**. Bei der Inbetriebnahme über `adq_json_override` einstellen (Parametersatz
  `ADQ_PARAMETER_ID_PULSE_GENERATOR0` bzw. `PORT_GPIOA`; Schlüssel mit `GetParametersString` auslesen).

**Ablauf je Lauf (beide live):** Rollen und Pegel aus dem Laufplan setzen (`arm`), dann ADQ32 starten, dann
HydraHarp starten. Die Daten werden im Strom geschrieben: PTU je Lauf und ein Block in `digitizer.h5` (gzip,
wachsende Datensätze). Ein abgebrochener Lauf wird verworfen; abgeschlossene Läufe bleiben erhalten.

## 3 Betriebsarten und Kennzeichnung

Jedes Gerät läuft für sich als **sim**, **replay** oder **live**:

* `device_mode` gilt für beide Geräte; `harp_mode` und `digitizer_mode` überschreiben ihn einzeln.
* sim und replay lassen sich nicht mischen. live mit sim oder mit replay ist erlaubt.
* Sitzungsart: sim, replay, live oder **mixed**.
* Sobald ein Gerät live ist, gilt Echtzeit mit Tempo 1.
* Gemischte Sitzungen werden **nicht automatisch ausgewertet**, weil ihre Daten physikalisch nicht
  zusammengehören. Für Prüfungen kann man sie von Hand auswerten.

Die Betriebsart steht in **jeder** Ausgabedatei:

| Datei | Kennzeichnung |
|---|---|
| PTU je Lauf | Tags `UsrHHD_DeviceMode` (dieses Gerät), `UsrHHD_DeviceModes` (JSON beider Geräte), `UsrHHD_AcquisitionMode`; `HW_Type` ohne „(SIMULATED)“, wenn die Harp live ist |
| `digitizer.h5` | Wurzelattribute `device_mode_harp`, `device_mode_digitizer`, `acquisition_mode`; dieselben Attribute je Block; Geräteattribut `device_mode` |
| `session.json` | `device_modes`, `acquisition_mode`, `hardware` (Modell, Seriennummer, Firmware/Bibliothek), `simulated` (false, sobald ein Gerät live ist) |
| `server_session.json` | dazu `fallback` (Rückfall auf Simulation, falls geschehen) |

Die Web-App zeigt in der Kopfzeile je Gerät ein Abzeichen: **LIVE** (rot), **SIM**, **REPLAY**. Die Sitzungsart
steht als SIMULIERT, WIEDERGABE, LIVE oder GEMISCHT darüber. Bei gemischtem Betrieb oder Rückfall erscheint
zusätzlich ein Banner. Auch die Sitzungsliste nennt die Betriebsart.

## 4 Konfigurationsbeispiele

`config/server.toml` dokumentiert alle Einträge. Fertige Beispiele:

* `config/server_live.toml`: beide Geräte live am Mess-PC, mit Pause zwischen den Läufen für die Inbetriebnahme.
* `config/server_mixed.toml`: HydraHarp live, Digitizer simuliert (z. B. bevor der ADQ32 eingebaut ist).
* `config/adq_override.example.json`: Form des `adq_json_override`, eine Liste von Parametersätzen
  `{"id": "ADQ_PARAMETER_ID_…", …}`. Sie wird nach der Grundeinstellung eingemischt.

```toml
[devices]
harp_mode = "live"
digitizer_mode = "replay"           # z. B. HydraHarp-Test gegen eine aufgezeichnete Digitizer-Sitzung
replay_dir = "hhd_data/HHD-20261017-120000"

[live]
live_fallback = "refuse"            # oder "sim"
live_pause_between_runs = true
hh500_lib = ""                      # $HHD_HH500LIB
adq_marker_capture = "channel_b_sync"
```

Befehlszeile: `python -m hhd.server --config config/server_live.toml`, oder einzeln
`--harp-mode live --digitizer-mode sim`.

## 5 Sicherheit (ED-8b)

* Kritische Geräteparameter (Eingangsbereiche, Pegel, Offsets, Referenztakt, Recordlänge, Marker) lassen sich
  am Mess-PC direkt ändern, aus der Ferne nur **zweistufig**: Der Server liefert einen Code, der 120 s gilt und
  an Client und Änderung gebunden ist.
* Triggerausgänge sind aus der Ferne nie schaltbar.
* **Start einer Sitzung mit Live-Gerät aus der Ferne** braucht ebenfalls eine zweistufige Bestätigung
  (`live_remote_start_confirm`, Standard an). Die App zeigt dafür einen Bestätigungsdialog.
* Steuersperre (ein steuernder Client) und Befehlsprotokoll `logs/commands.jsonl` gelten unverändert. Darin
  stehen auch abgelehnte Befehle; Bestätigungscodes sind maskiert.
* `live_pause_between_runs`: Vor jedem Lauf wartet der Server im Zustand **pause** auf „Weiter“
  (`continue_run`), z. B. um Blocker oder Trigger umzustecken. Ein doppeltes „Weiter“ überspringt keine Pause.

## 6 Inbetriebnahme – Prüfliste

### Ablauf am Mess-PC (Windows)

1. **Treiber installieren** (Abschnitt 1): PicoQuant HydraHarp-500-Software/-Bibliothek (`hh500lib64.dll`, USB) und
   Teledyne SP Devices ADQAPI (`ADQAPI.dll`, PCIe-Treiber). Liegen die Bibliotheken nicht im Suchpfad:
   `HHD_HH500LIB` / `HHD_ADQAPI` setzen oder `hh500_lib` / `adq_lib` in `config/server_live.toml` eintragen.
2. **Geräteprüfung** – lädt die Treiber, öffnet und fragt die Geräte ab, **ohne zu messen**, und schließt sie wieder:

   ```bat
   .venv\Scripts\python -m hhd.server.check --config config\server_live.toml   :: oder: hhd check-devices
   ```

   Je Gerät: Treiberpfad und -version, Gerät gefunden, Modell, Seriennummer, Firmware; HydraHarp zusätzlich
   Eingänge, Sync-Rate und Basisauflösung, ADQ32 Abtastrate und Kanäle. Fehler mit Abhilfe auf Deutsch:
   Treiber fehlt → Umgebungsvariable bzw. Konfigurationseintrag setzen; Gerät belegt → andere Software
   (PicoQuant-Software, ADQAssist/Digitizer Studio, zweiter Server) schließen; kein Gerät → USB bzw. PCIe,
   Netzteil und Gerätemanager prüfen. Rückgabewert 0 nur, wenn alle als live konfigurierten Geräte in Ordnung sind
   (1 Fehler, 2 Konfiguration fehlerhaft, 3 nichts live konfiguriert). Zusammenfassung:
   `<log_dir>/geraetepruefung.json` (bzw. `--json …`); `--alle` prüft auch simuliert eingestellte Geräte.
   Dieselbe Prüfung: `GET /api/devices/check` (`?alle=true`; nur ohne laufende Sitzung, sonst HTTP 409) und Knopf
   **„Geräte prüfen“** in der Ansicht *Geräte* der Web-App. Der Server schließt dafür seine Live-Geräte kurz und
   öffnet sie danach wieder.
3. **`start_messung.bat`** (Doppelklick): führt die Geräteprüfung aus und zeigt das Ergebnis. Bei einem Fehler
   bricht es mit Hinweis ab. Sonst startet es den Server mit `config/server_live.toml` (nur 127.0.0.1), erzeugt
   einen Zugangscode und öffnet den Browser am Mess-PC. Optionen:
   * `-Online`: zusätzlich Tunnel + QR-Code wie `start_online.bat`;
   * `-Trotzdem`: nur für gemischten Betrieb. Geräte, die die Prüfung nicht bestehen, werden ausdrücklich als
     simuliert gestartet (`--harp-mode sim` bzw. `--digitizer-mode sim`, Warnung in der Konsole, Kennzeichnung in
     App und Dateien); mindestens ein Live-Gerät muss in Ordnung sein. Einen stillen Rückfall auf Simulation gibt
     es nicht (`live_fallback = "refuse"`).
   * Außerdem: `-Port`, `-Config`, `-NoBrowser`, `-NoWait`.

   Beenden: Enter im Fenster, `stop_messung.bat` oder `stop_online.bat`.
   Fehlt Laufwerk `D:` (`data_dir`/`log_dir` = `D:/hhd_…`), weist das Skript darauf hin. Server und Prüfung legen
   die Daten dann unter `.\hhd_data` bzw. die Logs unter `.\logs` ab (Meldung „Hinweis: Laufwerk D: nicht
   vorhanden …“).

Vorbereitung:

1. Bibliotheken installieren (Abschnitt 1). `python -m pytest tests/test_drivers.py` muss grün sein (Mocks).
   Danach mit `HHD_HH500LIB`/`HHD_ADQAPI` auf die echten Bibliotheken zeigen und die Versionen ausgeben.
2. Verkabelung prüfen:
   * gemeinsame 10-MHz-Referenz an Harp und ADQ32 (`ref_clock = "extern_10MHz"` an beiden);
   * Herald an Harp CH1 und ADQ32 TRIG;
   * SYNC-Marker an Harp-Sync und ADQ32 SYNC;
   * Triggerkopie (GPIO) an Harp CH2;
   * Homodyn an Kanal A;
   * PDA05CF2 mit 10-dB-Dämpfer an Kanal B.
3. Geräteprüfung bestehen, dann Server am Mess-PC mit `start_messung.bat` bzw. `config/server_live.toml` starten
   (`live_pause_between_runs = true`). Erst `status` ansehen: Seriennummern, Kanalzahl, Bibliotheksversionen,
   keine `device_error`.

Am Gerät zu prüfen (**[unsicher]** im Code):

4. **HydraHarp:**
   * Grenzwerte für Pegel, CFD und Offsets sowie die Fehlercodes (`hh500defin.h`);
   * Sync-Teiler 1 in T2;
   * PTU-Recordtyp der HydraHarp-500-T2-Records: eine Live-PTU in der PicoQuant-Software öffnen und mit
     `python -m hhd.cli import <sitzung>` gegenlesen.
5. **ADQ32:**
   * `GetParametersString(TOP)` sichern;
   * JSON-Schlüssel und Enum-Namen mit dem Treiber vergleichen;
   * angenommene Eingangsbereiche und DC-Offset-Grenzen;
   * `adq_num` = 1 nach `SetupDevice`;
   * ob die Zeitstempel bei jedem Start zurückgesetzt werden;
   * `time_unit` aus dem Header gegen 1/(8·f_ADC).
6. **Marker-Erfassung über Kanal B mit SYNC-Trigger:** Ein kurzer K-V-Lauf muss in `sync/sync.json`
   `n_pairs == n_markers_H` und eine Periode von 1 ms zeigen. Klappt das nicht, auf `channel_b_trig`
   ausweichen und die Marker anders erfassen.
7. **Triggerkopie an CH2:** Den Pulsgenerator per `adq_json_override` einrichten. Die Synchronisation darf die
   Warnung „CH2-Kopie bestätigt nur …“ höchstens am Laufrand zeigen. Außerdem prüfen, ob CH2 in periodischen
   Läufen ebenfalls Kopien liefert (der Mock kopiert dort Heralds).
8. Pegel ohne Licht (Dunkellauf) und mit LO prüfen: LO-Monitor-Mittelwert, Abstand zu ±1,75 V, keine
   Übersteuerung (Statusbit `OVERRANGE`).
9. Kurzplan `kurz` live aufnehmen und offline auswerten:
   `python -m hhd.cli sync <sitzung>` und `python -m hhd.cli compare <sitzung>` bzw. „neu auswerten“ in der App. Vakuumvarianz und LO-Stabilität prüfen
   (`hhd.analysis.driftmon.DriftMonitor`, `docs/SIMULATOR.md` Abschnitt Datenstrom).
10. Fernzugriff zuletzt: Token setzen, Start aus der Ferne mit Bestätigung prüfen, Protokoll kontrollieren.

## 7 Tests

* `tests/test_drivers.py`: Treiber gegen die Mocks (Laden, fehlende Bibliothek, Parameterprüfung, FIFO/Overflow,
  T2-Decoder).
* `tests/test_server_live.py`: Server live+live und gemischt (beide Richtungen):
  * Dateien und Betriebsart in PTU, h5 und Manifest;
  * Marker über Kanal B und Synchronisation der Live-Daten;
  * Start aus der Ferne mit Bestätigung;
  * fehlende Bibliothek: Verweigerung bzw. Rückfall;
  * Pause zwischen Läufen;
  * Overflow- und FIFO-Fehler;
  * Beispielkonfigurationen.
* `tests/test_check.py`: Geräteprüfung gegen die Mocks (in Ordnung; Bibliothek fehlt; kein Gerät; Gerät belegt –
  `MOCK_HH500_BUSY`, `MOCK_ADQ_BUSY`), Rückgabewerte und JSON, `hhd check-devices`, API inkl. Sperre während einer
  Sitzung, Laufwerk-Rückfall, statische Prüfung von `start_messung.ps1`/`.bat` (ohne PowerShell).
* Die Mocks werden je Testlauf mit dem vorhandenen C-Compiler gebaut. Ohne Compiler werden diese Tests
  übersprungen.
