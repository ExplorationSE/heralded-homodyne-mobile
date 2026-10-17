# Änderungsprotokoll – ExSE Harp ADQ32 HHD Mobil

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
