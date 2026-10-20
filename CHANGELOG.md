# Änderungsprotokoll – ExplorationSE Harp ADQ32 HHD Mobil

## 0.15.0.dev0 – 20.10.2026 (unveröffentlicht)

Auftrag „Geräteprüfung und Start am Mess-PC“:

- Startdateien für alle Fälle: `geraete_pruefen.bat`, `start_messung_nur_harp.bat`, `start_messung_nur_adq32.bat` (mit `config/server_live_nur_harp.toml` bzw. `server_live_nur_adq32.toml`).
- **Geräteprüfung ohne Messung** (`hhd/server/check.py`): `python -m hhd.server.check --config config/server_live.toml`
  bzw. `hhd check-devices`.
  - Lädt die Treiber, öffnet und fragt HydraHarp 500 und ADQ32 ab und schließt sie wieder.
  - Meldet je Gerät: Treiberpfad/-version, gefunden, Modell, Seriennummer, Firmware; Eingänge/Sync bzw.
    Abtastrate/Kanäle.
  - Fehler mit Abhilfe auf Deutsch (Treiber fehlt / Gerät belegt / kein Gerät).
  - Rückgabewert 0 nur bei allen Live-Geräten in Ordnung; JSON-Zusammenfassung `geraetepruefung.json`.
- **API und App:**
  - `GET /api/devices/check` (nur ohne laufende Sitzung, sonst 409; Live-Geräte des Servers werden kurz
    geschlossen und wieder geöffnet; Eintrag im Protokoll; `status.device_check`);
  - Knopf „Geräte prüfen“ in der Ansicht *Geräte*.
- **`start_messung.bat`/`.ps1`, `stop_messung.bat`:**
  - Ablauf: Prüfung → Abbruch mit Hinweis, sonst Server mit `server_live.toml`, Zugangscode, Browser lokal;
  - `-Online` (Tunnel + QR); `-Trotzdem` nur gemischter Betrieb mit ausdrücklich simuliertem Gerät.
- **Fehlendes Laufwerk** in `data_dir`/`log_dir` (z. B. `D:`): Rückfall auf `.\hhd_data` bzw. `.\logs` mit Meldung,
  bei Prüfung und Serverstart.
- **Treiber und Mocks:**
  - `ADQAPILib.revision()` (optional `ADQAPI_GetRevisionString`);
  - Mocks: belegtes Gerät (`MOCK_HH500_BUSY`, `MOCK_ADQ_BUSY`) und Revisionstext.
- **Tests:** `tests/test_check.py`.

Auftrag „Phasenabhängigkeit“ (Entscheidung Herr Eilers: zuerst im Simulator, Zielvariante Piezo-Rampe im LO-Arm):

- **Simulator** (`hhd/sim/phase.py`, `phase.mode = "piezo"`):
  - LO-Phase aus einer Piezo-Rampe (Dreieck/Sägezahn, Frequenz, Hub in λ);
  - Nichtlinearität, Hysterese je Richtung, Drift, Zufallsdrift, Phasenrauschen.
  - Die Rampenspannung je Record wird aufgezeichnet (`ramp_channel = "aux_adc"` → `records/ramp_raw`); ADQ32-Kanal B bleibt LO-Monitor.
  - Neuer Lauf K-θ (`phase_ref`, kohärente Referenz).
  - Wahre Phase je Record `theta_true` in `truth.h5` und `truth_records.h5`.
  - Sollwerte phasenempfindlicher Zustände aus den wahren θ.
- **Datenformat:**
  - `digitizer.h5` Schema 1.3: optionale Record-Spur `ramp_raw`, Datensatztyp `phase_reference`; 1.0–1.2 bleiben lesbar.
  - `analysis/phase.h5`: θ, σ_θ und Herkunft (none/ramp/locked/estimated) je Record.
  - `RunData.read_ramp()`.
- **Kalibrierung K-θ** (`hhd/calib/phase_cal.py`):
  - Polynom Spannung → θ je Rampenrichtung (Hysterese), Versatz je Referenzlauf, Drift interpoliert;
  - σ_θ je Record; Güte und Warnungen.
- **Auswertung** (`hhd/analysis/phase.py`, `hhd/recon/tomo.py`):
  - MaxLik-Tomografie (RρR) mit bekannter Phase, Verlust η in der POVM, automatischer Fock-Grenze;
  - 2D-Wigner; Quetschung (dB, Achse); Var(x_θ) mit Fit; Parität; α; Treue zum Sollzustand;
  - Phasenabdeckung mit Lückenwarnung; Bootstrap.
  - Speicherbegrenzt über ein θ×x-Histogramm, auch in der Datenstrom-Auswertung.
  - Phasengemittelt bleibt der Fallback; Bericht- und Ergebnisabschnitt „phase“.
  - Neue `AnalysisParams`: `phase`, `phase_cal_deg`, `theta_sign`, `theta_noise_prior_rad`, `tomo_*`, `n_boot_tomo`, `var_theta_bins`, `wigner2d_points`.
- **Validierung:** θ gegen die Wahrheit; ρ gegen ρ_soll (Verlust, ξ, Dephasierung) mit Treue und Pulls.
- **Laufpläne** `kurzphase_<zustand>` (9 Zustände), Gruppe „Mit Phase (Piezo-Rampe)“.
- **Web-App:** 2D-Wigner aus ρ, Var(x_θ), Phasenabdeckung, Kennwerttabelle, Kennzeichnung „Phase bekannt (Piezo-Rampe, kalibriert)“ bzw. „phasengemittelt“.
- **Tests:** `tests/test_phase.py` (13): Rampe/Schema 1.3, θ-Kalibrierung gegen die Wahrheit, MaxLik für kohärent/gequetscht/Katzen, Quetschachse, Datenstrom, alte Dateien, |1⟩ unverändert, POVM-Adjungierte.
- Keine neuen Python-Abhängigkeiten (numpy, scipy, h5py wie bisher).

## 0.14.0.dev0 – 19.10.2026 (unveröffentlicht)

Auftrag „weitere Zustände im Laufplan“ (Wunsch Herr Eilers):

- **Neue geheraldete Zustände im Simulator** (`hhd/sim/states.py`, `STATE_PRESETS`):
  - Fock |2⟩, |3⟩ und die Überlagerung (|0⟩+|1⟩)/√2;
  - gerade/ungerade Katzen mit α = 1 und α = 2, das Kätzchen 3 dB (photonensubtrahiertes gequetschtes Vakuum);
  - kohärent |α=1⟩, gequetschtes Vakuum 3 dB/6 dB, thermisch n̄ = 0,5.
- **Laufpläne `kurz_<zustand>`** (`hhd/sim/state_plans.py`) für „Datei (im Speicher)“ und „Datenstrom“:
  - Aufbau wie „kurz“ mit A 2 × 3 s, z. B. „Kurz: Fock |2⟩ (7 Läufe)“;
  - die Auswahlliste der Web-App ist gruppiert (`plan_groups`, optgroup).
- **Phasengemittelt:** Phasenempfindliche Zustände laufen mit zufälliger LO-Phase.
  - Sollwerte, Auswertung und Darstellung sind phasengemittelt.
  - Die App kennzeichnet das beim Zustand und an der Wigner-Karte.
- **Zustand in Metadaten und Ergebnis:**
  - Laufattribut `heralded_state`, `server_session.json` (`state`, `plan_label`);
  - Sitzungsliste, `result["state"]`, Kopfzeile des Berichts, Ergebnisansicht.
- **Auswertung für Zustände ≠ |1⟩:**
  - η aus (Var x − ½)/⟨n⟩_ideal (`eta_n`, Budget `eta_eff_n`), η_rest beruht darauf;
  - der |1⟩-Fit und die |1⟩-Treue sind dort als nicht gültig gekennzeichnet bzw. ausgeblendet;
  - `validate-analysis` prüft eta_n statt eta_fit.
  - Für |1⟩ bleibt alles unverändert.
- **Tests:** `tests/test_state_plans.py`:
  - MC gegen ρ (Varianz, W(0,0), Histogramm) für alle Voreinstellungen;
  - Var(θ) des gequetschten Vakuums;
  - jeder neue Laufplan durch Simulation und Auswertung mit Sollwertvergleich;
  - Datenstrom mit Katze; |1⟩ unverändert.

Auftrag („weiter“): Farbe, PCA im Datenstrom, Langlauf mit 1000/s, speichersparendes `validate-records`.

- **Web-App-Farben:** Rosa (#f472b6) ist durch Orange (#f97316) ersetzt, das zum Blau #38bdf8 passt und sich
  vom Warn-Bernstein #f59e0b abhebt. Betroffen sind die A-/Herald-Kurven, die Rate, W(r), p_n, das A/B-Histogramm,
  die positiven Werte der Wigner-Karte und das Icon. App-Screenshots und die Bedienungsanleitung sind neu erzeugt.
- **PCA/Modenüberlapp in der Datenstrom-Auswertung:**
  - inkrementelle Fensterkovarianz (Σw, Σwwᵀ je Block) mit neuem gemeinsamem Kern
    `modes.pca.window_sums`/`pca_from_sums`;
  - gleiche Auswahl und Obergrenze wie im Speicher (`AnalysisParams.pca_max_records`, Standard 20 000,
    0 = alle, `--pca-max-records`), daher gleiches Ergebnis (rel. 10⁻⁷);
  - η_Mode (PCA) erscheint jetzt im Effizienzbudget der Datenstrom-Auswertung.
- **`hhd validate-records` speichersparend:** Die PTU-Datei wird blockweise dekodiert (`herald_ps_stream`),
  im Speicher bleiben nur die Herald-Zeiten. Beim Langlauf sank der Bedarf von 497 auf 194 MB.
- **Langlauf mit Herald 1000/s über den Server** (`config/longrun_1k.yaml`, `docs/SERVER.md` Abschnitt 7c):
  - 1,37 h, 5,34 Mio. Records, 0 Fehlzuordnungen von 4,53 Mio., alle Pulls |z| < 2;
  - Spitzenspeicher: Server 277 MB, Auswertung 291 MB; 15,2 GB Daten (danach gelöscht);
  - PCA über alle 4,39 Mio. A-Fenster: Überlapp 0,9993, τ_fit 40,0 ns.
- Umbenennung.
- **Tests:** `test_longrun.py::test_streaming_pca_matches_in_memory` (Obergrenze 20 000, 300, alle) und
  `test_streaming_sim.py::test_validate_records_streams_ptu`.

## 0.13.0.dev0 – 18.10.2026 (unveröffentlicht)

Auftrag („Los.“): speichersparende Δ-Kalibrierung, echte Mehrstunden-Simulation über den Server.

- **Δ-Kalibrierung als Summen je Rasterpunkt** (`delay.GridAccumulator`, `SyncParams.delay_method = stream`,
  `hhd sync --delay-method stream|matrix`):
  - Q² wird blockweise sofort akkumuliert; Bootstrap als Poisson-Bootstrap (n_boot × n_grid Summen);
  - Speicher unabhängig von der Kandidatenzahl, Ergebnis unabhängig von der Blockung;
  - V(δ) und Δ_T identisch zur früheren Matrixvariante, σ_boot im Rahmen des Bootstrap-Rauschens;
  - 60-s-Lauf, alle 65 868 Kandidaten: 413 → 66 MB (Spitze der ganzen Sync), 36 → 21 s.
- **Zuordnungswahrheit je Record** im Datenstrom-Simulator (`truth_records.h5`, `record_truth_dir`,
  Server `sim_record_truth`), Prüfung `hhd validate-records` (`hhd.sim.record_validate`), auch für
  segmentiert synchronisierte Läufe.
- `truth.h5` des Datenstrom-Simulators enthält Sollwerte je Lauf (`expected_json`, recordgewichtetes Mittel über
  die Scheiben), damit läuft `hhd validate-analysis` auch für Datenstrom-Sitzungen.
- **Fix DriftMonitor:** im Dunkellauf (LO aus) keine LO-Warnung mehr (vorher „LO-Leistung −621 %“ bei
  Dunkelläufen länger als das Fenster).
- **Fix Server-Ende:** der Auswertungsprozess (ProcessPool) wird beim Beenden mit beendet, statt verwaist
  weiterzulaufen.
- `config/longrun_2h.yaml` (2,26-h-Sitzung für den Server).
- **Mehrstunden-Simulation über den Server** (Port 8795, `sim_engine = stream`, segmentierte Sync auto,
  Auswertung, Drift-Monitor): `docs/SERVER.md` Abschnitt 7b. Ergebnis: 0 Fehlzuordnungen von 1,46 Mio.,
  alle Pulls |z| < 1, Spitze 236 MB (Server) bzw. 265 MB (Auswertung), 4,9 GB Platte.
- **Tests:** `tests/test_delay_stream.py` (4), `test_streaming_sim.py::test_record_truth_validation`,
  Erweiterungen in `test_server.py::test_stream_sim_engine` und `test_longrun.py::test_drift_over_replay`.

## 0.12.0.dev0 – 18.10.2026 (unveröffentlicht)

Auftrag („Weitermachen“): Datenstrom-Simulator als Server-Gerät, segmentierte Synchronisation sehr langer
Einzelläufe, Behebung der RuntimeWarning in `hhd/sync/delay.py`.

- **Server, Simulator im Datenstrom** (`StreamSimBackend`, `sim_engine = batch | stream`):
  - wählbar in `server.toml`, mit `--sim-engine` und in der Web-App (Auswahl „Simulator“, Abzeichen
    „SIMULIERT · DATENSTROM“, Geräteinfo, Status, `server_session.json`);
  - Dateien über `SessionRecorder`, `truth.h5` dezimiert;
  - Speicher konstant: Standard-Laufplan ×4 85 MB gegen 841 MB im Batch.
  - `StreamingSimulator` hat eine schrittweise Schnittstelle `setup` / `iter_run` / `write_truth`.
- **Segmentierte Synchronisation** `hhd/sync/chunked.py` (`SyncParams.chunked = auto|always|never`, ab 600 s;
  `hhd sync --chunked-sync`):
  - Segment-Uhrfit, stückweise Korrektur, Verfeinerung über Paare;
  - Segmentversatz über δ_HT angeglichen;
  - Δ aus einer Reservoir-Stichprobe;
  - `SegmentedClock` mit Überblendung;
  - blockweises Schreiben von `sync.h5`.

  Gegen Batch: 0 Fehlzuordnungen, gleiches Δ_T, Modenort rms ~0,1 ns. Speicher konstant.
- `refine_clock` aus `_refine` herausgelöst; `write_sync` in Kopf- und Laufgruppen zerlegt. `sync_session` lädt
  lazy.
- **Fix:** Δ-Raster ohne `RuntimeWarning: Mean of empty slice` (`delay.col_mean`, Rasterpunkte außerhalb aller
  Records sind NaN ohne Warnung).
- **Tests:** `tests/test_sync_chunked.py` (5), `test_server.py::test_stream_sim_engine`.

## 0.11.0.dev0 – 17.10.2026 (unveröffentlicht)

Auftrag: Wiedergabe und Auswertung langer Sitzungen mit beschränktem Speicher (`docs/ANALYSE.md` Abschnitt 12).

- **Lesen in Zeitfenstern** `hhd/io/chunked.py`: PTU, Digitizer-Köpfe, Marker (mit Übertrag zwischen Blöcken).
- **Lazy Laufdaten:** `load_session(lazy=True)`, Köpfe als Eigenschaften, `RunData.release()`, `iter_headers`;
  `sync_session` gibt Läufe nach der Zuordnung frei (`SyncParams.release_runs`).
- **Wiedergabe** (`ReplayBackend`) als Folge von Zeitfenstern: konstanter Speicher, vollständig (vorher fehlten
  Records vor dem ersten Marker), PTU bitgleich.
- **Datenstrom-Auswertung** `hhd/analysis/streaming.py` (`hhd compare --streaming [--chunk N]`):
  - zwei Durchgänge über `quadratures.h5`;
  - Kalibrierung aus Potenzsummen;
  - Histogramme `QHist` mit exakten Momenten;
  - Bin-MaxLik, Poisson-Bootstrap, KS/χ², Nulltests.
- **Server:** `streaming_records` (Standard 200 000) wählt die Datenstrom-Auswertung für Aufträge.
- **Drift über Wiedergabe** `hhd/analysis/replaydrift.py`, `hhd drift`. `DriftMonitor` projiziert in Teilblöcken
  (vorher Spitze ~780 MB bei 1-s-Fenstern).
- Refaktor: `drift_ab_from_per`, `crosstalk_check(kh_stats=…)`.
- **Tests** `tests/test_longrun.py` (5); Messwerkzeug `tools/longrun_memory.py`.

## 0.10.0.dev0 – 17.10.2026 (unveröffentlicht)

Auftrag („Los.“): echte Treiber für HydraHarp 500 und ADQ32 hinter dem Live-Platzhalter, speichersparender
Simulator für den Drifttest (`docs/HARDWARE.md`). Nur öffentliche Handbücher, keine Anfrage an den Hersteller.

- **Treiber** `hhd/drivers/` (ctypes, Bibliothek erst beim Öffnen geladen; Suchreihenfolge Konfiguration →
  `HHD_HH500LIB`/`HHD_ADQAPI` → Standardorte; fehlt sie, `LibraryNotFound` mit Installationshinweis):
  - **HH500Lib:** T2, Referenztakt, Sync/Eingänge (ETR/CFD, Flanke, Pegel, Offsets), Sync-Teiler, Marker,
    Messung mit CTC, FIFO-Auslese, Flags/Warnungen, Debug-Dump, T2-Decoder.
  - **ADQAPI:** Versionsprüfung, JSON-Parameter mit Typerhalt, `configure_homodyne`:
    - Kanal A homodyn mit TRIG/PERIODIC, Kanal B LO-Monitor mit Sample Skip;
    - Marker (ED-7) über Kanal B mit SYNC-Trigger (`markers/data_b`);
    - `WaitForRecordBuffer`, Header 2.0, Override-JSON.
- **Mock-Bibliotheken** in C (`hhd/drivers/mock/`, Bau mit gcc/cc/clang) mit gleicher Schnittstelle und
  erzwingbaren Fehlern (kein Gerät, SYSERROR, FIFO voll, Overflow, falsche Version).
- **Live-Geräte** `hhd/server/livedev.py`: Auslese-Thread, Gerätezeitfenster je Lauf, Fehler als `DeviceError`.
- **Gemischter Betrieb:** je Gerät sim/replay/live (`harp_mode`, `digitizer_mode`, `--harp-mode`,
  `--digitizer-mode`); Sitzungsart sim/replay/live/mixed.
  - Die Betriebsart steht in jeder Datei: PTU-Tags `UsrHHD_DeviceMode…`, h5-Attribute, `session.json`,
    `server_session.json`.
  - Die Web-App zeigt sie als Abzeichen LIVE/SIM/REPLAY je Gerät, mit Banner bei gemischtem Betrieb oder Rückfall.
  - `live_fallback` = refuse | sim.
  - Gemischte Sitzungen werden nicht automatisch ausgewertet.
- **Aufzeichnung im Datenstrom** `hhd/io/recorder.py`, `stream_writer.py`: PTU je Lauf, `digitizer.h5`
  (Schema 1.2, erweiterbare Datensätze), `session.json` mit Hardware; ein abgebrochener Lauf wird verworfen.
- **Schema 1.2:** `markers/data_b` bei `channel_b_trigger = "SYNC"`. Import und Analyse interpolieren den
  LO-Monitor an den Triggerzeiten.
- **ED-8b:**
  - Start mit Live-Gerät aus der Ferne nur zweistufig bestätigt;
  - Pause zwischen Live-Läufen mit „Weiter“ (`continue_run`);
  - Steuersperre und Protokoll wie bisher.
- **Datenstrom-Simulator** `hhd/sim/streaming.py`: Zeitscheiben, lazy Irrfahrt/Drift, dezimierte Wahrheit.
  Der Spitzenspeicher hängt nicht von der Laufdauer ab (2,2 MB bei 2 × 2 s bis 2 × 8 s).
- **Drift-Überwachung** `hhd/analysis/driftmon.py`: LO aus Kanal B, Vakuumvarianz und η, konstanter Speicher.
  - Test: LO +2 % erkannt (+1,84 %, z = 52) und η-Drift erkannt (z = −5,3).
  - Kontrolle ohne Drift: kein Alarm.
- Konfiguration `[live]` in `config/server.toml`, Beispiele `server_live.toml`, `server_mixed.toml`,
  `adq_override.example.json`.
- Tests `tests/test_drivers.py`, `tests/test_server_live.py`, `tests/test_streaming_sim.py`.
- Behoben: Ein doppeltes „Weiter“ konnte die nächste Pause überspringen.
- Live-Vorschau: Ein Dunkelwert, der sich kaum vom LO-Wert unterscheidet (LO nicht abgeschaltet?), wird mit
  Warnung verworfen. Im gemischten Betrieb gibt es keine Sync-Warnungen, weil die Geräte nicht gekoppelt sind.

## 0.9.0.dev0 – 17.10.2026 (unveröffentlicht)

Auftrag von Herrn Eilers („Bitte.“, Phase 2): Server und App nach GX-Vorbild (`docs/SERVER.md`).

- **Mess-Server** `hhd/server/` (FastAPI, `python -m hhd.server` / `hhd-server`, `config/server.toml`):
  Geräteabstraktion HydraHarp 500 und ADQ32 je **live** (Platzhalter mit SDK-Abbildung), **sim** (Simulator im
  Datenstrom, Echtzeitfaktor) und **replay** (Sitzung aus Dateien, ohne `truth.h5`); **Messablauf** einer Sitzung
  nach Laufplan (M1 Zeitraffer 18 Läufe, Kurzplan 7 Läufe) mit Stopp; Dateien wie `hhd.io.session`.
- **Live-Synchronisation** (`StreamingSynchronizer` je Lauf) und **Live-Vorschau** (Quadraturhistogramm, Varianz je
  Lauf normiert auf K-V, LO-Monitor mit Dunkelwert, Raten, Warnungen), Aufzeichnung `live_preview.json`.
- **Offline-Auswertung per Auftrag** (eigener Prozess: Synchronisation + `analyze_session`), automatisch nach der
  Sitzung oder auf Befehl; Ergebnis-Kurzfassung und Bericht über `/api`.
- **Steuersperre**, **Befehlsprotokoll** `logs/commands.jsonl`, **Zugangscode** (Cookie/`?token=`,
  Fehlversuchsgrenzen, Origin-Prüfung, Sicherheits-Header), **ED-8b**: kritische Parameter nur am Mess-PC oder mit
  zweistufiger Bestätigung, Triggerausgänge nie aus der Ferne; Tunnelverbindungen gelten als entfernt.
- **Web-App** (deutsch, mobil, ohne Fremdbibliotheken): Messung, Ergebnisse (Wigner-Funktion, W(r), p_n,
  Vergleich A/B, Effizienzbudget, Bericht), Geräte, Protokoll; Kennzeichnung SIMULIERT.
- `tools/start_online.sh` (cloudflared), `tools/app_screenshots.py` (Playwright).
- Tests `tests/test_server.py` inkl. **Ende-zu-Ende** mit simulierten Geräten (Sitzung → Dateien → Sync →
  Analyse → Ergebnis in der API der App), Stopp, Wiedergabe, Zugang.

## 0.8.0.dev0 – 17.10.2026 (unveröffentlicht)

Auftrag von Herrn Eilers („Bitte.“, Phase 1): Statistik nachschärfen (`docs/ANALYSE.md`, Abschnitt 11).

- **Konfidenzintervalle** `hhd/recon/intervals.py`: Perzentil, basic, normal, biaskorrigiert-normal, BCa
  (Beschleunigung aus Gruppen-Jackknife), studentisiert; alle in `ci_all`, Standard je Größe in `CI_DEFAULT`
  (**normal**), Felder `*_ci68`, `*_ci95`, `*_ci_method`, `*_ci_params`.
- **Kampagne 200 Sitzungen** (+ Neuauswertung der 64 aus 0.7): Abdeckung kombiniert W(0,0) 68,2/94,7 %,
  η-Fit 68,2/93,6 %, Var x 67,0/96,6 % – Sollwert erreicht; die Unterdeckung in 0.7 war im Wesentlichen
  Stichprobenstatistik der 64 Sitzungen. W(0,0)-Rest +0,0028 ± 0,0006 = Positivitätsrand des ML-Schätzers
  (Erwartung +0,0018), Momente unverzerrt.
- **OP-5** Nulltests A-A/B-B (`comparison.null_tests`): p-Werte gleichverteilt (KS p = 0,50/0,65), A-B in 100 %
  getrennt; Warnungen bei |z| > drift_z bzw. p < drift_p.
- **OP-6** Driftkampagnen (LO linear/Sprung 2 %, η linear −5 %/Sprung −10 %), `tools/drift_thresholds.py`
  (Fehlalarm, Erkennung, Hochrechnung auf Echtzeit); Standards `lo_rel_warn = 0,5 %`, `drift_z = 3`, neu
  `drift_p = 0,0027` (χ² der A-/B-Läufe, A-B-A, Nulltests).
- **Dunkellauf** im M1-Laufplan (10 s, LO aus; 18 Läufe), Simulator/Planstatistik (`T_dark`, `rate_dark`,
  `dark_end`, `sigma_var_el_rel`), η_el mit Fehler im Effizienzbudget.
- Werkzeug `tools/campaign_analysis.py` neu (Abdeckung je Methode, Indikatoren, `--fast`, `--procs`).
- Tests: Intervalle, KI-Standard, Nulltests, η_el-Fehler, Dunkellauf im Plan, Driftschwellen-Werkzeug.

## 0.7.0.dev0 – 17.10.2026 (unveröffentlicht)

Auftrag von Herrn Eilers („Weitermachen“): Normierung/Kalibrierung, Modenextraktion, Rekonstruktion, Vergleich A/B
auf Basis von `sync.h5` (Beschreibung `docs/ANALYSE.md`).

- **Modenextraktion** `hhd/modes/`: Modenzentren aus Herald + Δ bzw. Trigger + Δ_T, Vorlagenbank mit
  Unterabtastlage und Bandbreitenmodell, Abdeckung, ADC-Nullpunkt aus K-V; **PCA** der bedingten Kovarianz
  (C_A − C_KV) mit Überlapp, Rauschanteil, τ-/Versatzfit; Option Mode aus PCA (`pca`, `pca_raw`).
- **Kalibrierung** `hhd/calib/`: Vakuumnormierung (gepoolt, interpoliert, LO-Monitor je Lauf/Record),
  elektronisches Rauschen aus Dunkellauf oder Clearance-Parameter, Kanal-B-Dunkelwert, Driftindikatoren (K-V,
  LO-Monitor), K-H-Übersprechprüfung (Modenquadratur und Wellenform).
- **Rekonstruktion** `hhd/recon/`: MaxLik (RρR), EM, Histogramm-MaxLik, Momentenschätzer, Verlustmatrix,
  Wigner-Funktion (radial, Gitter), Photonenstatistik, Fidelity, η-Fit; θ-Schnittstelle (`phase.py`).
- **Kette** `hhd/analysis/`: `analyze_session` → `analysis/result.json` (Schema `hhd-result/1.0`),
  `quadratures.h5`, `report.md`; gemeinsamer Bootstrap mit neu gezogener Vakuumkalibrierung (**OP-1 gelöst**),
  Bootstrap-Biaskorrektur und Momenten-Gegenprobe für W(0,0) (**OP-2 Ursache: Positivitätsrand des
  ML-Schätzers, ~1/N**), Vergleich A/B (Histogramme, KS, Differenzen, Wigner-Differenz, η aus Var_A − Var_B),
  Drift A/B (A-B-A, A-Hälften), Effizienzbudget.
- **CLI** `hhd calibrate|reconstruct|compare|report|validate-analysis`.
- **Validierung** `hhd/sim/analysis_validate.py` (nur dort Wahrheit), Studien `tools/campaign_analysis.py`,
  `tools/op1_vacuum_bootstrap.py`, `tools/op2_w00_bias.py`; Ergebnisse in `docs/studien/`.
- **Tests** `tests/test_analysis.py` (Schätzer, Vorlagen, Drift, Kette, Normierungen, PCA, Dunkellauf, Clearance,
  Übersprechen, keine Wahrheit, CLI).

## 0.6.0.dev0 – 17.10.2026 (unveröffentlicht)

Auftrag von Herrn Eilers: Import und Synchronisation (Gesamtplan v1.7, Synchronisationsbericht).

- **Import** `hhd/io/session.py`: `load_session`/`load_campaign` (Schemaprüfung, SHA-256), Manifest-Whitelist
  (`expected`, `config`, `seed` werden verworfen), `truth.h5` gesperrt (`PermissionError`); `RunData` mit Harp-Strom
  nach Kanalrollen (`UsrHHD_ChannelRoles`/`UsrHHD_MarkerRoles`), Trigger-/Sample-/Markerzeiten des ADQ32,
  Records Kanal A und `data_b` blockweise; PTU chunkweise (`read_ptu_header`, `iter_ptu_records`).
- **Synchronisation** `hhd/sync/`: Marker-Gitterindex mit fortschreitendem Periodenfit, Kreuzkorrelation über
  das Auslassmuster (Eindeutigkeit), robuste Gerade und stückweise lineare Korrektur mit Kreuzvalidierung,
  Verfeinerung über Herald/Trigger-Paare (gewichtete Steigung, optional stückweise), Zuordnungsklassen
  (zugeordnet, verwaist, mehrdeutig, Zufalls-/Zustandstrigger über CH4, periodisch; Heralds zugeordnet/sekundär/
  ungetriggert mit Grund Totzeit/Rand/unerklärt), überlappende Fenster, CH2-Prüfung (abgewiesene Trigger),
  Lauf B/K-V mit Zeitachse ohne Herald-Zuordnung, Δ-Feinkalibrierung über das Maximum der bedingten
  Quadraturvarianz (Δ_T gepoolt, Δ je Lauf = Δ_T + δ_HT, Bootstrap), Selbstprüfung mit Warnungen; Ausgabe
  `sync/sync.h5` und `sync/sync.json`.
- **Datenstrom** `hhd/sync/stream.py`: `StreamingSynchronizer` (Einrasten, Nachführen, Zuordnung mit Latenz).
- **Schnittstelle Rust-Kern (AP9):** `hhd.sync.kernels` (zustandslose Array-Funktionen), Wahl über
  `HHD_SYNC_BACKEND`.
- **Validierung** `hhd/sim/sync_validate.py` (nur mit Wahrheit): Markerpaare, Pulls a/b, Abbildungsfehler,
  Fehlzuordnungsrate, Verluste, Klassenkonsistenz, Δ- und Δ_T-Pull.
- **CLI** `hhd` (`import`, `sync`, `validate`, `stream`, `bench`), `python -m hhd`.
- **Tests** `tests/test_import.py`, `tests/test_sync.py` (gemeinsame 10 MHz, freie Uhr mit Markern am M3,
  Irrfahrt mit stückweisem Fit, fehlende/zusätzliche Marker, falscher Versatz, Stresstest 200 kHz, Kampagne,
  Datenstrom = Batch, keine Wahrheit, CLI). Doku `docs/SYNC.md`.

## 0.5.0.dev0 – 17.10.2026 (unveröffentlicht)

Entscheidungen von Herrn Eilers zum Vorschlag M1: 1550 nm, LO-Monitor PDA05CF2 + 10 dB, Herald-Detektor SNSPD
(Alternative InGaAs-APD), Laufplan und Laufmarke wie vorgeschlagen.

- **Abschnitt `optics`:** `wavelength_nm` 1550 (Metadatum in PTU-Kopf, Manifest, HDF5), `herald_detector`
  (`snspd` | `ingaas_apd` | `custom`), `homodyne_pd` InGaAs-PIN, `eta_hd_qe` 0,95 (Prüfung η ≤ eta_hd_qe).
- **Herald-Presets:** SNSPD η_det 0,85, R_d 100 /s, Jitter 40 ps FWHM, Totzeit 30 ns; InGaAs-APD 0,20,
  3 kHz, 150 ps, 10 µs. Neue Quellparameter `det_eff`, `herald_arm_T` (0,6), `det_dead_s`; η_h wird abgeleitet
  (0,51). Presets füllen nur nicht angegebene Felder.
- **Totzeit des Herald-Detektors** (nicht-paralysierbar) in der Engine; Warnung bei Totzeit·Rate > 5 %.
- **LO-Monitor-Kette** `lo_monitor_levels`: PDA05CF2 (1,0 A/W, 5 kV/A, 2 mV rms), Dämpfer 10 dB, Abgriff 150 µW,
  DC-Offset −0,2 V → Kanal-B-Pegel; Prüfung gegen Abs. Max. ±1,75 V und DC-Offset ±0,25 V.
- **Default-Laufplan = M1-Plan** (`M1_PLAN`, wie `config/laufplan_m1.yaml`) im Zeitraffer 1/90, 17 Läufe;
  Statistik in `plan.py` mit R_d 100 /s.
- Vorlage `config/ingaas_apd.yaml` (30 kHz Herald-Rate); Tests `tests/test_optics.py`; Drifttests in
  `test_session.py` nutzen die frühere kurze Sitzung.

## 0.4.0.dev0 – 17.10.2026 (unveröffentlicht)

Vorschlag M1 (Laufplan, LO-Photodiode, Laufmarke) für Herrn Eilers.

- **Stochastische Drift** `hhd/sim/drift.py`: Irrfahrt und Sinus für LO-Leistung und η, Drift der
  Monitor-Verstärkung (`lo_monitor.gain_rw_per_sqrt_s`), Wahrheit in `truth.h5:/drift`; Allan-Abweichung und
  Rauschmodell-Fit; Szenarien gut/typisch/schlecht [Annahme]. Bestehende Sitzungen bleiben bitgleich.
- **Laufplan** `hhd/sim/plan.py`: Blockfolge, Statistik, Datenvolumen, Driftfehlermodell je Normierung,
  Anpassungsregel `adapt_plan` (AP14); Kommando `hhd-sim plan` (auch `--allan` für eine Stabilitätsmessung);
  Vorlage `config/laufplan_m1.yaml`.
- **Schnelle Studie** `hhd/sim/study.py` (Monte Carlo auf Quadraturebene) und `tools/laufplan_studie.py`.
- **Auswertung:** ABA-Vergleich bei `--norm interpolated` mit K-V-Statistik (vorher Fehlalarme).
- Tests `tests/test_plan_study.py`.

## 0.3.0.dev0 – 17.10.2026 (unveröffentlicht)

Entscheidungen von Herrn Eilers zu Gesamtplan v1.4: Lauf B freilaufend, LO-Monitor an ADQ32-Kanal B,
HydraHarp 500 S mit 4 Eingängen plus Sync-Eingang.

- **Lauf B Standard `free_running`** (freilaufender interner Trigger); Zufallstrigger als Vorlage `random_b.yaml`
  (ersetzt `free_running.yaml`).
- **Kanalbelegung HydraHarp 500 S:** Sync-Eingang (Kanal 0, Special-Record) = Sync-Marker, CH1 Herald,
  CH2 Kopie der ADQ32-Trigger, CH3 reserviert (zweiter Herald/PNR), CH4 Hilfstrigger (Zustand/Zufall),
  M1 Laufmarke, M2 Phasenreferenz, M3/M4 frei; Rollen im PTU-Kopf und in HDF5; Prüfung auf Doppelbelegung,
  Sync-Teiler, Marker-Bits.
- **ADQ32:** Pulsgenerator am GPIO kopiert die Triggerereignisse (TRIG bzw. periodisch, Raster 6,4 ns) an Harp CH2.
- **LO-Monitor an Kanal B:** `records/data_b` (gleicher Trigger, Sample Skip 25, 200 Samples), Abschnitt
  `lo_monitor`; Auswertung `--norm monitor` und Driftindikator nutzen Kanal B (Dunkelwert aus `electronic`-Läufen);
  externer 10-Hz-Leistungsmesser optional (`drift.monitor`, Standard aus).
- HDF5-Schema 1.2 (additiv); Tests `tests/test_channels.py`.

## 0.2.0.dev0 – 17.10.2026 (unveröffentlicht)

Entscheidungen von Herrn Eilers zu Gesamtplan v1.3 (ED-12, ED-11, ED-14, HydraHarp 500 S).

- **Messablauf ED-12:** getrennte Läufe als Standard, verschachtelt K-V → K-H → A (Herald) → B (ohne Herald) → A → K-V;
  Laufart `free_running` (freilaufender Trigger) neben `random`; eingestreute Zufallstrigger nur noch als Option.
- **Sitzung/Kampagne:** Abschnitt `session` (ersetzt `run`), Lauf-IDs, gemeinsame `calibration_id`, Manifest
  `session.json` (ersetzt `run.json`), `hhd-sim campaign` mit `campaign.json`; IDs in HDF5 (Schema 1.1) und PTU-Kopf.
- **Drift:** Abschnitt `drift` (LO-Leistung, Effizienz), Sprünge je Lauf (`lo_scale`, `eta_scale`), LO-Monitor.
- **Auswertung:** Vakuumnormierung `pooled` / `interpolated` / `monitor`; Vergleich A/B; Driftindikatoren mit Warnungen.
- **Defaults:** HydraHarp 500 S mit 4 Kanälen; ADQ32-PCIe S2G5 (2,5 GS/s), Bandbreitenoption BW1G0 (1 GHz) mit
  Tiefpassmodell.
- Tests `tests/test_session.py`; Vorlagen `drift.yaml`, `free_running.yaml`, `offherald_option.yaml`.

## 0.1.0.dev0 – 17.10.2026 (unveröffentlicht)

Beginn AP2 Simulator (Vorarbeit vor Projektstart 20.10.2026), Weiterentwicklung des Prototyps `sync_sim`.

- Paket `hhd` nach dem Muster von ExplorationSE Harp GX Mobil (MIT, CI-Workflow für Ubuntu/Windows, Python 3.11/3.13).
- `hhd.io`: PTU Generic T2 (HydraHarp 500), HDF5-Schema v1 für ADQ32-Records.
- `hhd.sim`: Konfiguration mit Parametertabelle, Zustände (Vakuum, kohärent, thermisch, Fock |1⟩ mit η; Fock |n⟩,
  Katze als Erweiterung), Phase θ (zufällig, fest, Rampe, Stufen; ED-11), Uhrenmodell, Gerätemodelle HydraHarp 500
  und ADQ32-PCIe, Kontrollmessungen K-V/K-H, Zufallsfenster, Ground Truth, Datenstrom-Modus, Kommandozeile.
- Tests: PTU-Roundtrip (eigener Read_PTU-kompatibler Leser, optional gegen das offizielle Demo), Gerätegrenzen,
  Zustandsmomente, W(0,0)-Validierung, bekannte Phase, Übersprechen-Nachweis über K-H, Datenstrom, Kommandozeile.
