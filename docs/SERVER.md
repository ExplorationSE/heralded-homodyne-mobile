# Mess-Server und Web-App (`hhd.server`)

Stand 0.10.0.dev0 – Architektur nach dem Vorbild von ExplorationSE Harp GX Mobil (`harp/server.py`, `control.py`,
`sources/base.py`, `auth.py`, Web-App; nur gelesen, nicht verändert), erweitert auf zwei Geräte und den
HHD-Messablauf (Gesamtplan v1.7, ED-8b, ED-12).

## 1 Überblick

```
Smartphone / Browser  ──HTTPS/WSS (LAN oder Tunnel)──▶  Mess-PC: python -m hhd.server
  Web-App (deutsch, mobil)                                ├─ Geräte: HydraHarp 500, ADQ32  (live | sim | replay)
  /ws: Status, Live-Vorschau, Befehle                     ├─ Messablauf: Sitzung nach Laufplan (K-V, Dunkel, K-H, A-B-A …)
  /api: Sitzungen, Ergebnisse, Bericht, Protokoll         ├─ Live-Synchronisation + Live-Vorschau
                                                          ├─ Dateien: Sitzung wie hhd.io.session (PTU, digitizer.h5)
                                                          └─ Auswertung als Auftrag (ensure_sync + analyze_session)
```

Die Web-App spricht **nur** mit dem Server. Geräte, Rohdaten und Auswertung bleiben auf dem Mess-PC.

| Modul | Inhalt |
|---|---|
| `hhd/server/devices.py` | Geräteabstraktion `Device` (open/configure/arm/start/read/stop), Parameterbeschreibung `ParamSpec` mit `critical`/`local_only`; HydraHarp 500 und ADQ32 je als **live** (`livedev.py`, Treiber `hhd/drivers/`, siehe `docs/HARDWARE.md`), **sim** (Simulator im Datenstrom-Modus, Echtzeitfaktor) und **replay** (Wiedergabe einer Sitzung aus Dateien; `truth.h5` wird nie übernommen) |
| `hhd/server/livedev.py` | Live-Geräte `HydraHarp500Live` (HH500Lib, T2) und `ADQ32Live` (ADQAPI): Bibliothek erst beim Öffnen laden, Auslese-Thread, Laufzeitfenster, Fehler als `DeviceError` |
| `hhd/io/recorder.py` | `SessionRecorder`: Live/gemischt im Datenstrom schreiben (PTU je Lauf, `digitizer.h5` Schema 1.2, `session.json` mit Betriebsart und Hardware) |
| `hhd/server/live.py` | Live-Vorschau: `StreamingSynchronizer` je Lauf (Einrasten, Anteil zugeordneter Heralds), Quadratur je Record mit nomineller Mode am Ort Trigger + Δ_T, Normierung auf die bisherigen K-V-Läufe (Vakuum = ½), Histogramm, Varianz je Lauf mit Fehler, LO-Monitor (Kanal B, Dunkelwert aus dem Dunkellauf), Raten, Warnungen |
| `hhd/server/service.py` | Messdienst: Konfiguration (`config/server.toml`), Messablauf-Thread, Stopp (abgeschlossene Läufe werden geschrieben), Auswertungsaufträge in eigenem Prozess, Sitzungsliste, Ergebnis-Kurzfassung |
| `hhd/server/control.py` | Steuersperre, Befehlsprotokoll (`logs/commands.jsonl`), Zugangscode, ED-8b-Rechte (`ParamPolicy`), Pfadschutz |
| `hhd/server/app.py` | FastAPI: Web-App, WebSocket `/ws`, REST `/api/*`, Anmeldung, Sicherheits-Header |
| `hhd/server/web/` | Web-App (HTML/CSS/JS ohne Fremdbibliotheken, Canvas-Grafiken, PWA-Manifest) |

## 2 Start

```bash
pip install -r requirements.txt                       # fastapi, uvicorn, python-multipart, httpx
python -m hhd.server --config config/server.toml       # oder: hhd-server --config …
# Optionen: --host, --port, --mode live|sim|replay, --harp-mode, --digitizer-mode, --replay-dir <sitzung>,
#           --data-dir, --log-dir, --token, --speed
python -m hhd.server --config config/server_live.toml    # beide Geräte live (docs/HARDWARE.md)
python -m hhd.server --config config/server_mixed.toml   # HydraHarp live, Digitizer simuliert
```

Dann im Browser `http://<mess-pc>:8766/` (LAN; Port 8766, GX nutzt 8765). Am Mess-PC selbst `http://127.0.0.1:8766/`.

* **LAN:** `host = "0.0.0.0"`; ohne Zugangscode nur in einem geschützten Netz.
* **Online (temporär):** Zugangscode in `config/server.toml` setzen und `tools/start_online.sh` (cloudflared
  Quick-Tunnel, Server nur auf 127.0.0.1). Über den Tunnel verbundene Clients gelten immer als **entfernt**.
* **Betriebsart je Gerät** (`device_mode`, einzeln `harp_mode`/`digitizer_mode`): sim, replay oder live;
  gemischt live + sim bzw. live + replay (nicht sim + replay). Die Kopfzeile zeigt je Gerät **LIVE**/**SIM**/
  **REPLAY** und die Sitzungsart SIMULIERT, WIEDERGABE, LIVE oder GEMISCHT; gemischter Betrieb und ein Rückfall
  auf die Simulation stehen zusätzlich in einem Banner. Mit Live-Gerät gilt Tempo 1 (Echtzeit).
* **Fehlende Herstellerbibliothek:** klare Meldung mit Installationshinweis; `live_fallback = "refuse"`
  (Standard, Start gesperrt) oder `"sim"` (Rückfall, gekennzeichnet). Der Server stürzt nicht ab.
* **Simulation/Wiedergabe:** Laufpläne `standard` (M1 im Zeitraffer,
  18 Läufe, ≈ 16 s mit Pausen) und `kurz` (7 Läufe) sowie ab 0.14 `kurz_<zustand>` für weitere
  geheraldete Zustände (Fock |2⟩/|3⟩, Katzen, Kätzchen, kohärent, gequetscht, thermisch, Überlagerung;
  phasengemittelt; Gruppen in `plan_groups`); `speed` = Echtzeitfaktor.

## 3 Bedienung (Web-App)

* **Messung:** Steuerung übernehmen, Laufplan und Tempo wählen, Sitzung starten/stoppen; Laufplanleiste
  (erledigt/aktuell), Fortschritt; Quadraturhistogramm des laufenden Laufs mit Vakuum-Gauß, Varianz je Lauf (Farbe
  nach Laufart, Vakuumlinie ½), LO-Monitor (±1-%-Band), Raten (Herald, Records), Synchronisation (eingerastet,
  Anteil zugeordnet), Warnungen.
* **Ergebnisse:** Sitzungsliste mit Auswertungsstatus; Tabelle A/B (N, Var x, W(0,0) mit 68-%-Intervall und
  Methode, η-Fit, ⟨n⟩, g², Fidelity, Differenzen A − B), Nichtklassizität in σ, Wigner-Funktion von A (aus W(r)),
  W(r) radial A/B/Vakuum, Photonenstatistik p_n, Quadraturhistogramme A/B mit KS und χ², Effizienzbudget,
  Bericht (`report.md`); „neu auswerten“.
* **Geräte:** alle Parameter beider Geräte mit Kennzeichnung *kritisch* / *nur Mess-PC*; Änderung nur mit
  Steuersperre; kritische Parameter aus der Ferne mit Bestätigungsdialog.
* **Protokoll:** alle Befehle (auch abgelehnte) mit Zeit, Client, Ort (Mess-PC/entfernt), Parametern, Ergebnis.

## 4 Rechte, Sperre, Protokoll (ED-8b)

| Aktion | Mess-PC (localhost ohne Proxy-Header) | entfernt (LAN, Tunnel) |
|---|---|---|
| Ansehen, Ergebnisse, Bericht, Protokoll | ja | ja (mit Zugangscode, falls gesetzt) |
| Start/Stopp, Laufplan, Tempo, Anzeige, Auswertung | mit Steuersperre | mit Steuersperre |
| Start einer Sitzung mit **Live-Gerät** | mit Steuersperre | mit Steuersperre **und Bestätigung** (`live_remote_start_confirm`) |
| „Weiter“ nach Pause zwischen Live-Läufen (`continue_run`) | mit Steuersperre | mit Steuersperre |
| kritische Geräteparameter (Eingangsbereiche, Triggerpegel, Referenztakt, DC-Offset, Sync-Teiler, Marker) | mit Steuersperre | mit Steuersperre **und Bestätigung** (zweistufig: Server liefert Code, 120 s gültig, nur für denselben Client und dieselbe Änderung) |
| Triggerausgänge | mit Steuersperre | **nie** |
| kritische Parameter während eines Laufs | nein | nein |

* **Steuersperre:** genau ein steuernder Client; frei bei Trennung, „Steuerung abgeben“ oder nach 300 s ohne Befehl.
* **Befehlsprotokoll:** `logs/commands.jsonl` (ts, client, addr, local, cmd, params, ok, msg); Bestätigungscodes
  werden maskiert.
* **Zugang:** Code per Anmeldeseite (HttpOnly-Cookie, SameSite=Lax, Secure hinter HTTPS) oder `?token=`;
  Fehlversuche je IP (8 / 10 min) und global begrenzt; WebSocket nur mit passender Origin; CSP,
  `X-Frame-Options: DENY`, `nosniff`, `noindex`.
* **Mess-PC-Erkennung:** Verbindung von 127.0.0.1/::1 **ohne** `CF-Connecting-IP`/`X-Forwarded-For`/… – der
  cloudflared-Tunnel verbindet ebenfalls von localhost und gilt deshalb als entfernt.

## 5 Protokoll der Schnittstelle

WebSocket `/ws`:

* Server → Client: `{"type":"hello","client","local","addr"}`, danach `{"type":"status", …}` mit `status_hz`
  (Standard 2 Hz) und sofort nach jedem Zustandswechsel: Version, Betriebsart, `simulated`, Zustand
  (`bereit|vorbereitung|messung|pause|schreiben|fertig|abgebrochen|fehler`), `mode`/`modes`/`mode_labels`
  (Betriebsart je Gerät und Sitzung), `live_any`, `fallback`, `waiting_continue`, Sitzung, Laufplan, Sperre, Anzeige,
  Geräte (Parameter, Spezifikation), Aufträge, `live` (aktueller Lauf mit Histogramm, alle Läufe, Verläufe, Warnungen).
* Client → Server: `{"type":"command","cmd":…,"params":{…},"id":n}` → `{"type":"ack","id":n,"ok":…,"msg":…}`.
  Befehle: `acquire_control`, `release_control`, `start_session {plan, speed, note, confirm?}` (mit Live-Gerät aus
  der Ferne zunächst `needs_confirm`), `stop_session`, `continue_run`,
  `set_display {plan, speed, auto_analyze, hist_range, n_boot}`, `set_device {device, params, confirm?}`
  (Antwort `needs_confirm` + `confirm`), `analyze {session}`, `clear_warnings`.

REST (lesend): `/api/health`, `/api/status`, `/api/sessions`, `/api/sessions/{id}/result` (vollständiges
`result.json`), `/api/sessions/{id}/summary` (Kurzfassung für die App), `/api/sessions/{id}/report`,
`/api/sessions/{id}/live` (Aufzeichnung der Live-Vorschau), `/api/log?n=`.

## 6 Dateien einer Server-Sitzung

`<data_dir>/HHD-JJJJMMTT-hhmmss/`: Sitzungsdateien wie `hhd.io.session` (PTU je Lauf, `digitizer.h5`,
`session.json`; in der reinen Simulation zusätzlich `truth.h5`, die von Import, Synchronisation und Auswertung
nicht gelesen wird), `server_session.json` (Laufplan, Geräteparameter, Betriebsarten, Hardware, Rückfall,
gestoppt?), `live_preview.json`, nach der Auswertung `sync/` und `analysis/` (`result.json`, `quadratures.h5`,
`report.md`, ab 0.15 bei bekannter Phase `phase.h5`). Die Betriebsart je Gerät steht in jeder Datei (PTU-Tags `UsrHHD_DeviceMode…`, h5-Attribute
`device_mode_*`/`acquisition_mode`, Manifest `device_modes`), siehe `docs/HARDWARE.md` Abschnitt 3. Live und
gemischt wird im Datenstrom geschrieben; ein abgebrochener Lauf wird verworfen, eine Sitzung ohne fertigen Lauf
entfernt.

## 7 Tests und Screenshots

* `tests/test_server_live.py`: live+live und gemischt gegen Mock-Bibliotheken (Dateien, Betriebsart, Marker über
  Kanal B, Synchronisation, Start aus der Ferne mit Bestätigung, fehlende Bibliothek, Pause, Overflow/FIFO).
* `tests/test_server.py`: Kurzplan im Datenstrom, Live-Gerät ohne Herstellerbibliothek, ED-8b-Regeln, Mess-PC-Erkennung, Sperre,
  Konfiguration, **Ende-zu-Ende** (WebSocket: Sperre → Sitzung starten → Dateien → Synchronisation → Auswertung →
  Ergebnis/Bericht/Live-Aufzeichnung über die API; Bestätigung kritischer Parameter; Triggerausgang abgelehnt;
  Protokoll; Sicherheits-Header), Stopp mitten in der Sitzung, Wiedergabe einer Sitzung, Zugangscode.
* `tools/app_screenshots.py <url> <ordner>`: headless Chromium (Playwright), mobil (390×844, als entfernter
  Client) und Desktop (Mess-PC).

## 7a Lange Sitzungen (0.11–0.13)

* **Simulator im Datenstrom** (0.12): `sim_engine = "stream"` (`server.toml`, `--sim-engine stream`, in der
  Web-App „Simulator: Datenstrom“, umschaltbar ohne laufende Sitzung).
  - Jeder Lauf wird in Zeitscheiben (≥ `chunk_s`, passend zu Marker- und Periodenraster) mit
    `hhd.sim.streaming` erzeugt.
  - Der Server schreibt die Dateien wie im Live-Betrieb (`SessionRecorder`), danach `truth.h5` (dezimiert) für
    die fertigen Läufe.
  - Die Betriebsart steht als Abzeichen „SIMULIERT · DATENSTROM“ in der Kopfzeile und an den Geräten, im Status
    (`sim_engine`) und in `server_session.json`.
  - Standard bleibt `batch` (ganze Sitzung im Speicher; alle Simulator-Merkmale).
  - Messung (Standard-Laufplan, ohne Auswertung, tracemalloc):

    | Simulator | ×1 | ×4 |
    |---|---|---|
    | batch | 261 MB | 841 MB |
    | stream | 84 MB | 85 MB |

  - Grenzen wie im Datenstrom-Simulator: keine Laufmarken und keine Phasenreferenz-Marker; Records an
    Scheibengrenzen entfallen.
  - `sim_record_truth = true` (0.13, nur `stream`): zusätzlich `truth_records.h5` mit der Zuordnungswahrheit je
    Record; Prüfung danach mit `hhd validate-records <sitzung>`.

* **Wiedergabe** (`ReplayBackend`): PTU (`hhd.io.chunked.iter_ptu_windows`), Digitizer-Köpfe und Marker werden in
  Zeitfenstern von `chunk_s` gelesen, Wellenformen je Fenster als zusammenhängender Ausschnitt. Der Speicher hängt
  nicht von der Laufdauer ab (×16-Sitzung: tracemalloc 19 MB, RSS 72 MB). Alle Records werden wiedergegeben, auch
  die vor dem ersten Marker; die PTU-Records sind bitgleich.
* **Auswertungsaufträge:** ab `streaming_records` Records (Standard 200 000, `[analysis]` in `server.toml`,
  0 = nie) läuft die Datenstrom-Auswertung (`docs/ANALYSE.md` Abschnitt 12), sonst die Auswertung im Speicher.
* `hhd drift <sitzung>`: Drift (LO, Vakuumvarianz, η) über eine Wiedergabe.

## 7b Mehrstunden-Simulation über den Server (0.13, 18.10.2026)

Ende-zu-Ende über einen eigenen Server (Port 8795, `realtime = false`, `sim_engine = "stream"`,
`sim_record_truth = true`, Auswertung automatisch, segmentierte Sync automatisch ab 600 s), Steuerung über
WebSocket wie die Web-App. Simulator-Konfiguration `config/longrun_2h.yaml`:
- 2,26 h simulierte Zeit, 9 Läufe: K-V 60 s, Dunkel 30 s, K-H 120 s, A 3600 s, B 300 s, K-V, A 3600 s, B, K-V;
- 1 792 928 Records à 2000 Samples, 13,4 Mio. Harp-Ereignisse;
- LO-Drift ±0,5 % (Periode 30 min) und η-Drift ±1 % (40 min) als Sinus.

| Schritt | Laufzeit (Box, 8 Kerne) | Spitzenspeicher (VmHWM) |
|---|---|---|
| Messung (Simulator → Dateien, Live-Vorschau) | 18,9 min (erster Lauf 13,3 min) | Server 236 MB |
| Auftrag: segmentierte Sync (2 × 120 Segmente) inkl. Δ | 6,6 min (Δ 84 s) | Auswertungsprozess 265 MB |
| Auftrag: Datenstrom-Auswertung | 1,6 min | (im selben Prozess) |
| `hhd validate-records` | 6 s | 403 MB |
| `hhd drift` (Wiedergabe, Drift-Monitor) | 2,3 min | 168 MB |

Plattenbedarf 4,9 GB (`digitizer.h5` 4,6 GB, `sync.h5` 95 MB, `quadratures.h5` 86 MB,
`truth_records.h5` 19 MB, PTU 2 × 33 MB). Der Speicher hängt nicht von der Dauer ab (Batch-Simulator und
Batch-Sync bräuchten für diese Sitzung mehrere GB).

Ergebnisse (gegen die Simulator-Wahrheit, `hhd validate-analysis`, `hhd validate-records`):
- Zuordnung: 0 Fehlzuordnungen von 1 464 248 zugeordneten Records, 0 verloren; Qualität „ok“, keine Warnungen.
- Δ_T = 77,23 ± 0,96 ns (Δ-Streaming, 20 000 Records, Poisson-Bootstrap).
- A (1 440 040 Records): Var x 0,8394 ± 0,0031 (wahr 0,8394), W(0,0) 0,1021 ± 0,0015 (wahr 0,1023),
  η_eff (Fit) 0,3396 ± 0,0023 (wahr 0,3394). Mit ξ = 0,500 und η_el = 0,969 folgt η_rest 0,7005 ± 0,0047
  (wahr 0,7002).
- B: W(0,0) 0,3171 ± 0,0013 (wahr 0,3183); Vergleich A − B: η 0,3375 ± 0,0026 (wahr 0,3394).
- Alle Pulls |z| < 1.
- Drift-Monitor: LO am Ende +0,64 % (wahr +0,64 %), Trend z = 3,6; η a_02/a_01 = 1,020 ± 0,021 (wahr 1,017).
  Nach dem Fix in 0.13 gibt es keine Fehlalarme mehr im Dunkellauf.
- Ein zweiter Durchlauf mit gleichem Seed lieferte dieselben Ergebnisse (Δ_T, Zählungen, Rekonstruktion).
- Grenze der Datenstrom-Auswertung: keine PCA (Modenüberlapp im Effizienzbudget „–“).

## 7c Langlauf mit realistischer Herald-Rate 1000/s (0.14, 19.10.2026)

Wie 7b: eigener Server (Port 8795, `realtime = false`, `sim_engine = "stream"`, `sim_record_truth = true`,
Auswertung automatisch, segmentierte Sync automatisch), Steuerung über WebSocket. Simulator-Konfiguration
`config/longrun_1k.yaml`:
- Herald 1000/s (+ Dunkel 100/s), 1,37 h simulierte Zeit, 9 Läufe: K-V 60 s, Dunkel 30 s, K-H 120 s,
  A 2000 s, B 300 s (1000/s), K-V, A 2000 s, B, K-V;
- 5 336 056 Records à 2000 Samples, davon 4 394 184 saubere A-Records;
- LO-Drift ±0,5 % (Periode 30 min) und η-Drift ±1 % (40 min) als Sinus.

| Schritt | Laufzeit (Box, 8 Kerne) | Spitzenspeicher (VmHWM) |
|---|---|---|
| Messung (Simulator → Dateien, Live-Vorschau) | 41,6 min | Server 277 MB |
| Auftrag: segmentierte Sync (a_01, a_02) inkl. Δ | 3,1 min | Auswertungsprozess 291 MB |
| Auftrag: Datenstrom-Auswertung inkl. PCA (20 000 Fenster je Gruppe) | 5,9 min | (im selben Prozess) |
| `hhd compare --streaming --pca-max-records 0` (PCA über alle 4,39 Mio. A-Fenster) | 7,0 min | 273 MB |
| `hhd validate-records` (0.14, PTU blockweise) | 17 s | 194 MB (0.13-Code: 497 MB) |
| `hhd drift` (Wiedergabe, Drift-Monitor) | 5,0 min | 186 MB |

Plattenbedarf 15,2 GB (`digitizer.h5` 14,3 GB, `sync.h5` 0,26 GB, `quadratures.h5` 0,27 GB,
`truth_records.h5` 60 MB, PTU 2 × 40 MB). Während der Sync lag für etwa 3 min eine Kopie von `digitizer.h5`
auf der Platte (Spitze 28,9 GB). Die Kopie stammte vom Sicherungsmechanismus der Box
(`*.box-store-snap-*`), nicht von hhd. hhd selbst legt keine Kopien an. Die Daten wurden danach gelöscht; die Kurzfassung liegt in
`/workspace/homodyne_herald/langlauf_0.14/`.

Ergebnisse gegen die Simulator-Wahrheit:
- Zuordnung: 0 Fehlzuordnungen von 4 529 235 zugeordneten Records, 0 verloren. Beide A-Läufe wurden
  segmentiert synchronisiert. Δ_T = 77,59 ns.
- A: Var x 1,1208 ± 0,0040 (wahr 1,1177, z +0,8), W(0,0) −0,0742 ± 0,0004 (wahr −0,0749, z +1,6),
  η_eff (Fit) 0,6187 ± 0,0018 (wahr 0,6177, z +0,6). Mit ξ = 0,909 und η_el = 0,969 folgt
  η_rest 0,7023 ± 0,0020 (wahr 0,7009, z +0,7).
- B: Var x 0,5039 ± 0,0021 (wahr 0,5001), W(0,0) 0,3160 ± 0,0012 (wahr 0,3182, z −1,8). Das ist dieselbe
  Abweichung wie bei Var x.
- PCA (Datenstrom, 20 000 Fenster): Überlapp roh 0,980, Rauschanteil 0,020, korrigiert 0,9999. Das
  Effizienzbudget zeigt η_Mode jetzt an (vorher „–“).
- PCA über alle Fenster: Überlapp roh 0,99934, Rauschanteil 0,0008, τ_fit 40,0 ns, Versatz 0,00 ns,
  λ₂/λ₁ 0,053.
- Drift-Monitor: LO kv_01 +0,78 % (wahr +0,78 %), kv_end −0,00 % (wahr −0,00 %); η a_02/a_01 0,994
  (wahr 1,000, Trend z −0,4). Einzige Warnung: „LO-Monitor: Lauf kv_01 weicht um +0.52 % von K-V ab“.
  Sie ist erwartet, denn die LO-Drift ist simuliert.
- Bei zehnfacher Herald-Rate gegenüber 7b (100/s) und 2,5-mal mehr Records bleibt der Speicher beschränkt:
  Server +17 %, Auswertung +10 %.

## 7d Laufpläne mit Phase (Piezo-Rampe, 0.15)

* Gruppe „Mit Phase (Piezo-Rampe)“ in der Auswahlliste: `kurzphase_<zustand>` für fock01, cat_even_1, cat_odd_1, cat_even_2, cat_odd_2, kitten_3dB, coherent_1, squeezed_3dB und squeezed_6dB.
  * Label z. B. „Kurz mit Phase: Katze gerade α=2 (10 Läufe)“.
  * Ablauf: K-V, Dunkel, K-θ, K-H, A (10 s), K-θ, B, A (10 s), K-θ, K-V.
* Die K-θ-Läufe erscheinen im Laufband in Gelb (`--kt`).
* `result_summary` enthält `phase`: Kennwerte, 2D-Wigner, Var(x_θ), Abdeckung, Kalibriergüte; ohne ρ.
* Ergebnisansicht bei bekannter Phase:
  * Kopfzeile „Phase bekannt (Piezo-Rampe, kalibriert)“;
  * Wigner-Karte als 2D-Wigner aus ρ (orange > 0, blau < 0);
  * Karte Var(x_θ): Daten mit Fehlern, Kurve aus ρ, Vakuum ½, Fit in dB und Achse;
  * Karte Phasenabdeckung: Lücken in Gelb, Kennwerttabelle mit Treue.
* Ohne Rampe bleibt die Darstellung phasengemittelt; die Begründung steht in der Kopfzeile.
* Geprüft bei 412 px Breite: Screenshots `app_screens/40_*.png`.

## 8 Grenzen

* Echte Geräte: Treiber nach Handbuch, nur gegen Mock-Bibliotheken getestet; offene Punkte in
  `docs/HARDWARE.md` Abschnitt 6.
* Gemischte Sitzungen werden nicht automatisch ausgewertet.
* Server-Simulation `batch`: Die Sitzung wird zu Beginn vollständig im Speicher simuliert und dann laufweise
  abgespielt. Für lange Sitzungen `sim_engine = "stream"` (Abschnitt 7a).
* Die Live-Vorschau nutzt die nominelle Mode und Δ_T (Vorschau, nicht die Auswertung).
* Kein Service-Worker (keine Offline-Nutzung), Grafiken als Canvas ohne Zoom.
