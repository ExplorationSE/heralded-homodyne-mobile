# Mess-Server und Web-App (`hhd.server`)

Stand 0.10.0.dev0 – Architektur nach dem Vorbild von ExSE Harp GX Mobil (`harp/server.py`, `control.py`,
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
  18 Läufe, ≈ 16 s mit Pausen) und `kurz` (7 Läufe); `speed` = Echtzeitfaktor.

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
`report.md`). Die Betriebsart je Gerät steht in jeder Datei (PTU-Tags `UsrHHD_DeviceMode…`, h5-Attribute
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

## 7a Lange Sitzungen (0.11)

* **Wiedergabe** (`ReplayBackend`): PTU (`hhd.io.chunked.iter_ptu_windows`), Digitizer-Köpfe und Marker werden in
  Zeitfenstern von `chunk_s` gelesen, Wellenformen je Fenster als zusammenhängender Ausschnitt. Der Speicher hängt
  nicht von der Laufdauer ab (×16-Sitzung: tracemalloc 19 MB, RSS 72 MB). Alle Records werden wiedergegeben, auch
  die vor dem ersten Marker; die PTU-Records sind bitgleich.
* **Auswertungsaufträge:** ab `streaming_records` Records (Standard 200 000, `[analysis]` in `server.toml`,
  0 = nie) läuft die Datenstrom-Auswertung (`docs/ANALYSE.md` Abschnitt 12), sonst die Auswertung im Speicher.
* `hhd drift <sitzung>`: Drift (LO, Vakuumvarianz, η) über eine Wiedergabe.

## 8 Grenzen

* Echte Geräte: Treiber nach Handbuch, nur gegen Mock-Bibliotheken getestet; offene Punkte in
  `docs/HARDWARE.md` Abschnitt 6.
* Gemischte Sitzungen werden nicht automatisch ausgewertet.
* Server-Simulation: Die Sitzung wird zu Beginn vollständig im Speicher simuliert und dann laufweise abgespielt.
  Für lange Läufe gibt es den speichersparenden `hhd.sim.streaming.StreamingSimulator` (`docs/SIMULATOR.md`
  Abschnitt 7); er ist noch nicht als Server-Gerät eingebunden.
* Die Live-Vorschau nutzt die nominelle Mode und Δ_T (Vorschau, nicht die Auswertung).
* Kein Service-Worker (keine Offline-Nutzung), Grafiken als Canvas ohne Zoom.
