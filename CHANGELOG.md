# Änderungsprotokoll – ExSE Harp ADQ32 HHD Mobil

## 0.1.0.dev0 – 17.10.2026 (unveröffentlicht)

Beginn AP2 Simulator (Vorarbeit vor Projektstart 20.10.2026), Weiterentwicklung des Prototyps `sync_sim`.

- Paket `hhd` nach dem Muster von ExSE Harp GX Mobil (MIT, CI-Workflow für Ubuntu/Windows, Python 3.11/3.13).
- `hhd.io`: PTU Generic T2 (HydraHarp 500), HDF5-Schema v1 für ADQ32-Records.
- `hhd.sim`: Konfiguration mit Parametertabelle, Zustände (Vakuum, kohärent, thermisch, Fock |1⟩ mit η; Fock |n⟩,
  Katze als Erweiterung), Phase θ (zufällig, fest, Rampe, Stufen; ED-11), Uhrenmodell, Gerätemodelle HydraHarp 500
  und ADQ32-PCIe, Kontrollmessungen K-V/K-H, Zufallsfenster, Ground Truth, Datenstrom-Modus, Kommandozeile.
- Tests: PTU-Roundtrip (eigener Read_PTU-kompatibler Leser, optional gegen das offizielle Demo), Gerätegrenzen,
  Zustandsmomente, W(0,0)-Validierung, bekannte Phase, Übersprechen-Nachweis über K-H, Datenstrom, Kommandozeile.
