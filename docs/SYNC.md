# Import und Synchronisation (AP Import, AP Synchronisation/Zuordnung)

Stand 0.6.0.dev0 – Python/NumPy-Referenzimplementierung; der Rust-Kern (AP9) übernimmt später die
Schnittstelle `hhd.sync.kernels`.

## 1 Überblick

```
PTU (T2, HydraHarp 500) ─┐                    ┌─ sync/sync.h5   (je Record/Herald: Klassen, Zuordnung, Zeitachse)
ADQ32-HDF5 (Schema 1.2) ─┼─ hhd.io.session ──► hhd.sync ──┤
session.json/campaign.json┘   (Import)          (Sync)    └─ sync/sync.json (Bericht, Selbstprüfung, Warnungen)

truth.h5 ───────────────────────────────────► hhd.sim.sync_validate  (nur Tests / `hhd validate`)
```

**Strikte Trennung:** Importer und Synchronisierer lesen nur Messdaten. `hhd.io.session` verweigert
`truth.h5` (`FORBIDDEN_FILES`, `PermissionError`) und übernimmt aus `session.json` nur eine Whitelist
(`SESSION_KEYS`, `RUN_KEYS`, Kalibrier-ID/-Modus, Dateiliste ohne Wahrheit); `expected`, `config` und `seed`
werden verworfen. Der Test `test_sync_no_truth` prüft das zur Laufzeit (Abfangen von `open`/`h5py.File`) und
statisch (Quelltext von `hhd/sync`).

## 2 Import (`hhd.io`)

| Quelle | Inhalt | Umsetzung |
|---|---|---|
| PTU T2 HydraHarp 500 | Kopf-Tags, Records (uint32), Überläufe, Marker | `ptu.read_ptu_header`, `ptu.iter_ptu_records` (chunkweise, `np.fromfile` ab Offset), `T2Decoder` (zustandsbehaftet über Chunkgrenzen) |
| Kanalrollen | `UsrHHD_ChannelRoles`, `UsrHHD_MarkerRoles` (JSON) | `HarpStream.role_s("herald" | "sync_marker" | "adq32_trigger_copy" | "random_tag" | "state_tag" …)`; Sync-Marker am Sync-Eingang (Kanal 0) oder am Marker-Eingang (M3) werden gleich behandelt |
| ADQ32-HDF5 1.2 | `device`-Attribute, `blocks/<id>/records/{timestamp, record_start, record_number, trigger_source, data, data_b}`, `markers/timestamp` | `RunData.trigger_D()`, `first_sample_D()`, `markers_D()`, `read_records(idx, channel="A"|"B")` (blockweise), `iter_records(chunk)` |
| Manifest | Läufe, Kalibrier-ID, Prüfsummen | `load_session(path, verify=True)` (Schemaprüfung, sha256 aller Dateien außer der Wahrheit) |
| Kampagne | `campaign.json` | `load_campaign`, `is_campaign` |

## 3 Synchronisation (`hhd.sync`)

Schichten: `kernels` (reine Array-Funktionen = Rust-Schnittstelle) → `markers`, `clockmap`, `assign`, `delay`
(Laufebene) → `pipeline` (Sitzung/Kampagne, Bericht) und `stream` (Datenstrom). Kernwahl über
`HHD_SYNC_BACKEND` (derzeit nur `python`).

### 3.1 Marker (Vorbericht 2, MARKER_PAARE)

1. **Gitterindex** `grid_index(t, P)`: Periode/Phase aus fortschreitenden Geradenfits (Präfix 2000 Marker,
   dann ×4), damit sich der Periodenfehler nicht aufsummiert (SYNC-Abstände sind auf 3,2 ns quantisiert).
   Marker abseits des Gitters (> P/4) werden verworfen (Störmarker am M3).
2. **Kreuzkorrelation** `xcorr_shift(kH, kD)` über das Auslassmuster (p_omit 10 %): Versatz s mit
   kH = kD + s. Eindeutigkeit u = Nebenmaximum/Hauptmaximum; Warnung bei u > 0,5.
3. **Paarung** `pair_by_index`.

### 3.2 Uhrabbildung (`clockmap`)

t_H = a0 + b·(T_D − x0) + Σ g_j(T_D)

* robuste Gerade (`robust_linear`, iteratives 5σ-Clipping, Kovarianz) – gemeinsame 10 MHz: b = 1;
  frei: b = 1/(1+ε);
* stückweise lineare Korrektur (Hutfunktionen, Bandmatrix `solveh_banded`, schwache Glättung); Knotenabstand
  W = T/2^j per Kreuzvalidierung (gerade/ungerade Paare, ≥ 20 Punkte je Knoten); übernommen nur, wenn der
  Vorhersagefehler < 0,9 × linear ist (Irrfahrt der Phase);
* Inverse `D(t_H)` per Newton.

### 3.3 Zuordnung (`assign`) und Verfeinerung

* δ_HT = Trigger − abgebildeter Herald (robuster Median der Nächste-Nachbar-Differenzen),
  Toleranz `max(6σ, 2 ns)`.
* **Segmentklassen:** zugeordnet (genau ein Herald im Fenster), verwaist (keiner), mehrdeutig (≥ 2; keine
  Rateentscheidung), Zufallstrigger/Zustandstrigger (genau eine CH4-Kennung), periodisch (Lauf B, K-V).
* **Heraldklassen:** zugeordnet, sekundär (Mode liegt in einem fremden Record), ungetriggert; Grund für
  fehlenden eigenen Record: Totzeit (Trigger fällt in [Trigger, letztes Sample + 1/f_s + Rearm + Toleranz]),
  Rand (Pretrigger vor Laufbeginn bzw. Record nach Laufende), unerklärt (→ Warnung > 1 %).
* je Record: `seg_n_secondary` (fremde Moden im Fenster), `seg_overlap_prev` (Samplefenster überlappt den
  vorigen Record – Pretrigger während der Totzeit).
* **Verfeinerung** über Herald/Trigger-Paare (CH1 ↔ ADQ32-TRIG-Zeitstempel, 50 ps): Steigung als
  inverse-varianzgewichtete Kombination Marker ⊕ Paare, Offset bleibt (Median entfernt), optional stückweise
  Korrektur (CV); danach zweite Zuordnung. Nicht heraldierte Läufe behalten die Markerabbildung.
* **CH2-Prüfung:** Kopie der ADQ32-Trigger (GPIO-Pulsgenerator) gegen die Record-Zeitstempel; Anteil
  bestätigter Records, Kopien ohne Record (= abgewiesene Trigger, Totzeit). Bei periodischen Triggern ist
  die Toleranz ≥ 1,5 × Pulsgenerator-Raster (6,4 ns).
* **Lauf B / K-V:** keine Herald-Zuordnung, aber Zeitachse (`record_time_H_s`, `record_time_session_s`) und
  Zahl der Heralds, deren Mode im Record liegt (`seg_n_heralds`).

### 3.4 Feinkalibrierung Δ (`delay`)

V(δ) = ⟨Q²(Trigger + δ)⟩ über saubere zugeordnete Records (keine sekundären Moden, keine Überlappung),
Q aus FFT-Korrelation mit der Modenfunktion (doppelt-exponentiell, 1/γ = 40 ns; Parameter `--gamma-ns`,
`--mode-shape`), normiert mit K-V-Läufen (Varianz 1/2; Ersatz: frühe Pretrigger-Samples).
Grobraster −700…1100 ns (8 ns), Feinraster ±120 ns (1 ns), Fit V = c + A·O(δ − Δ_T)² mit freier Basislinie,
Bootstrap (100). **Δ_T (Mode − Trigger)** wird sitzungsweit gepoolt; **Δ je Lauf (Mode − abgebildeter Herald)
= Δ_T + δ_HT**, weil die SYNC-Quantisierung (3,2 ns) die Phase der Abbildung je Lauf verschiebt.
Speicher: Records blockweise, gespeichert wird nur Q² auf dem Raster (≤ 20 000 Records).

### 3.5 Qualitäts-/Selbstprüfung

`sync.json → quality`: Status ok/warnung, Warnliste, Summen der Klassen und Gründe, max. σ_r der Marker,
max. Eindeutigkeit, max. Paarstreuung. Je Lauf: Markerzahlen/verworfene Marker, Versatz, u, Modell (linear/
stückweise, W), σ_r, Ausreißer, Residuen-RMS/-Maximum, Verfeinerung (σ vorher/nachher, Gewicht), δ_HT, σ_HT,
Toleranz, CH2-Prüfung, Δ. Warnungen: u > 0,5; < 10 Markerpaare; < 90 % zugeordnet (ohne CH4-Records);
mehrdeutig > 0,1 %; unerklärt ungetriggert > 1 %; CH2 < 99 %; Δ-Maximum nicht signifikant bzw. am Rasterrand.

### 3.6 Datenstrom (`stream.StreamingSynchronizer`)

Einrasten über die Batch-Markerzuordnung, sobald je 64 Marker vorliegen; danach Nachführen (neuer
Digitizer-Marker → nächster Harp-Marker über die aktuelle Abbildung, gleitender robuster Fit über 2000 Paare);
Records werden final, wenn ihr Trigger vor dem jüngsten gepaarten Marker liegt; δ_HT wird je Block
nachgeführt, bei Sprüngen neu geschätzt, lokal zwischen Nachbarn interpoliert. Heralds hinter dem ältesten
offenen Trigger werden verworfen. Stückweise Korrektur und Δ bleiben dem Batch-Lauf vorbehalten.

## 4 Ausgabe `sync/sync.h5`

`/runs/<block>/`: `seg_class` (int8, Codes in Attribut `seg_classes`), `seg_herald`, `seg_n_secondary`,
`seg_overlap_prev`, `herald_class`, `herald_reason`, `herald_segment`, `herald_D_s`, `record_time_H_s`,
`record_time_session_s`, `markers/{tH_s, TD_s, residual_s, used}`; Attribut `clockmap` (JSON, inkl.
stückweiser Anteile). Lauf B/K-V: `seg_class`, `seg_n_heralds`.

## 5 Kommandos

```
hhd import   <sitzung|kampagne>
hhd sync     <sitzung|kampagne> [-o DIR] [--no-delay] [--no-refine] [--linear] [--gamma-ns 40] [--strict]
hhd validate <sitzung|kampagne>          # nur Simulation: Vergleich mit truth.h5
hhd stream   <sitzung> [--run ID] [--chunk 1024]
hhd bench    <sitzung> [--repeat N] [--no-delay]
```
(`python -m hhd …`, falls das Skript nicht installiert ist.)

## 6 Validierung (`hhd.sim.sync_validate`, nur mit Wahrheit)

Markerpaare korrekt; Pulls für a, b gegen den LS-Fit der idealen, quantisierten Markerpunkte
(t_H = t_m − t0 + d_MH [+ Marker-Latenz] − ½ ps, T_D = ⌊clk(t_m + d_MD)/r⌋·r); Abbildungsfehler über
wahre Herald-Zeiten (Mittel = Quantisierungsphase bzw. Latenz; RMS, Maximum, auch nur Marker);
Fehlzuordnungsrate, Verluste, Klassenkonsistenz (Heralds ohne Record nie „zugeordnet“, CH4-Kennungen);
Δ-Pull gegen ⟨c_D − h_D⟩ (end-to-end), Δ_T gegen ⟨c_D − T_trig⟩.

## 7 Schnittstelle für den Rust-Kern (AP9)

`hhd.sync.kernels`: `robust_center`, `grid_index`, `xcorr_shift`, `pair_by_index`, `robust_linear`,
`piecewise_fit`, `piecewise_eval`, `nearest_diff`, `count_in_window`, `window_index` – zustandslos,
float64/int64-Arrays, Ergebnis als Dict. Der Rust-Kern implementiert diese Funktionen (PyO3) und wird über
`HHD_SYNC_BACKEND=rust` gewählt; `StreamingSynchronizer` ist die Vorlage für den zustandsbehafteten
Echtzeitkern (feed_harp/feed_digitizer/finalize).

## 8 Leistung (Box, 1 Kern, Python-Referenz, 17.10.2026)

| Fall | Ereignisse | Synchronisation ohne Δ | Durchsatz |
|---|---|---|---|
| Standardsitzung M1 (17 Läufe, 1 kHz Herald) | 56 252 | 0,23 s (mit PTU-Dekodierung) / 0,05 s | 2,5·10⁵ / 1,2·10⁶ Ereignisse/s |
| Stress 200 kHz Herald, 0,2 s | 129 475 | 0,15 s / 0,06 s | 8,6·10⁵ / 2,3·10⁶ Ereignisse/s |
| Stress 1 MHz Herald, 0,2 s (158 216 Records) | 596 095 | 0,57 s / 0,31 s | 1,0·10⁶ / 1,9·10⁶ Ereignisse/s |
| Datenstrom 1 MHz (Chunks à 8192 PTU-Records) | ≈ 3,6·10⁵ Heralds + Trigger | 0,06 s | ≈ 6·10⁶ Ereignisse/s |

Δ-Kalibrierung (Standardsitzung, 5133 Records à 5000 Samples, Grob- + Feinraster, Bootstrap 100): 3,2 s.
Ereignisse = Harp-Ereignisse aller Kanäle + Marker + ADQ32-Marker + Records. Die Echtzeitanforderung (1 kHz
Herald, Rust-Kern ab AP9) ist mit Faktor > 100 erfüllt.
