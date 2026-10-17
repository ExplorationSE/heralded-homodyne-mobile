# Änderungsprotokoll – ExSE Harp ADQ32 HHD Mobil

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

- Paket `hhd` nach dem Muster von ExSE Harp GX Mobil (MIT, CI-Workflow für Ubuntu/Windows, Python 3.11/3.13).
- `hhd.io`: PTU Generic T2 (HydraHarp 500), HDF5-Schema v1 für ADQ32-Records.
- `hhd.sim`: Konfiguration mit Parametertabelle, Zustände (Vakuum, kohärent, thermisch, Fock |1⟩ mit η; Fock |n⟩,
  Katze als Erweiterung), Phase θ (zufällig, fest, Rampe, Stufen; ED-11), Uhrenmodell, Gerätemodelle HydraHarp 500
  und ADQ32-PCIe, Kontrollmessungen K-V/K-H, Zufallsfenster, Ground Truth, Datenstrom-Modus, Kommandozeile.
- Tests: PTU-Roundtrip (eigener Read_PTU-kompatibler Leser, optional gegen das offizielle Demo), Gerätegrenzen,
  Zustandsmomente, W(0,0)-Validierung, bekannte Phase, Übersprechen-Nachweis über K-H, Datenstrom, Kommandozeile.
