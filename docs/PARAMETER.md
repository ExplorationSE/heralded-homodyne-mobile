# Parameter des Simulators (Defaults)

Erzeugt mit `hhd-sim params --markdown` aus `hhd/sim/config.py`. Status: **Spez.** = Herstellerunterlage, **Vorbericht** / Annahme / Vorschlag siehe Modulkopf.

| Parameter | Default | Einheit | Status | Quelle | Bedeutung |
|---|---|---|---|---|---|
| `session.id` | S001 |  | Vorschlag | ED-12 | Sitzungs-ID: eine Sitzung = Folge von Messläufen mit gemeinsamer Kalibrierung |
| `session.campaign` | K-SIM |  | Vorschlag |  | Kampagnen-ID (mehrere Sitzungen, z. B. Seeds-Kampagne oder Messtage) |
| `session.seed` | 20261020 |  | Vorschlag |  | Startwert des Zufallsgenerators; gleicher Seed = bitgleiche Dateien |
| `session.start_s` | 0.25 | s | Vorschlag |  | wahre Startzeit des ersten Laufs (Digitizer-Zähler läuft ab clock.t_arm_s) |
| `session.gap_s` | 0.5 | s | Vorschlag |  | Pause zwischen Läufen (Umbau, z. B. Blocker, Triggerquelle umschalten) |
| `source.R_herald` | 1000 | 1/s | Annahme | Vorbericht Synchronisation, Parameter | Rate echter Heralds (CW, Poisson) |
| `source.R_dark` | 20 | 1/s | Annahme | Vorbericht Synchronisation, Parameter | Dunkelzählrate des Herald-Detektors (falsche Heralds, Vakuum) |
| `source.eta_herald` | 0.5 |  | Annahme | Analyse Vergleich | Nachweiswahrscheinlichkeit im Herald-Arm; bestimmt die Rate ungeheraldeter Signalphotonen R_herald·(1/η_h − 1) |
| `source.eta` | 0.7 |  | Annahme | Vorbericht Synchronisation, Parameter | Zustandseffizienz (Optik·Photodiode·Modenanpassung) vor elektronischem Rauschen |
| `source.heralded_state` | fock1 |  | Vorschlag |  | Zustand der geheraldeten Mode (fock1; fock2 nur als Test) |
| `source.mode_shape` | double_exponential |  | Vorschlag |  | Temporale Mode: double_exponential f(t)=√γ·e^(−γ\|t\|) (CW-SPDC) oder gauss |
| `source.gamma` | 2.5e+07 | 1/s | Annahme | Vorbericht Synchronisation, Parameter | Abklingrate der Mode (1/γ = 40 ns); bei gauss: 1/σ |
| `source.sigma_spd` | 5e-11 | s | Annahme | Vorbericht Synchronisation, Parameter | Jitter des Herald-Detektors (SNSPD), gemeinsam für Harp und TRIG |
| `phase.mode` | random |  | Vorschlag |  | random (phasengemittelt), fixed, ramp (Sägezahn), steps |
| `phase.theta0` | 0 | rad | Vorschlag |  | Phase bei fixed bzw. Startphase von ramp/steps |
| `phase.ramp_Hz` | 10 | Hz | Annahme |  | Sägezahnfrequenz der Phasenrampe (2π je Periode) |
| `phase.steps` | [0.0, 0.7853981633974483, 1.5707963267948966, 2.356194490169959] | rad | Vorschlag |  | Phasenwerte bei steps |
| `phase.dwell_s` | 0.25 | s | Vorschlag |  | Verweildauer je Phasenstufe |
| `phase.marker_bit` | 2 |  | Vorschlag |  | Harp-Marker-Bitmaske für Rampen-/Stufenbeginn (0 = aus) |
| `harp.model` | HydraHarp 500 S |  | **Spez.** | HydraHarp 500 Manual, Kap. 9.3.1 | HydraHarp 500 S (4–8 Kanäle) oder M (8–16 Kanäle); Festlegung: S |
| `harp.n_channels` | 4 |  | **Spez.** | HydraHarp 500 Manual, Kap. 9.3.1 | Anzahl Eingangskanäle CH1..CHn (S: 4..8, M: 8..16), zusätzlich der Sync-Eingang (Kanal 0) |
| `harp.resolution` | 1e-12 | s | **Spez.** | HydraHarp 500 Manual, Kap. 9.3.1 | T2-Auflösung (fest 1 ps) |
| `harp.jitter_rms` | 2.5e-12 | s | **Spez.** | HydraHarp 500 Manual, Kap. 9.3.1 | Einzelmessungs-Zeitunsicherheit typ. (rms) |
| `harp.trigger` | ETR |  | **Spez.** | HydraHarp 500 Manual, Kap. 9.3.1 | Eingangstrigger ETR (Totzeit < 680 ps) oder CFD (< 6,8 ns) |
| `harp.tdead` | 0 | s | **Spez.** | HydraHarp 500 Manual, Kap. 6.1 | programmierbare Zusatz-Totzeit (0 = native Totzeit) |
| `harp.herald_channel` | 1 |  | Vorschlag | Gesamtplan v1.5, Kanalbelegung | CH1: Herald-Detektor (Kopie über Fan-out an ADQ32 TRIG) |
| `harp.trigger_copy_channel` | 2 |  | Vorschlag | Gesamtplan v1.5, Kanalbelegung | CH2: Kopie der ADQ32-Triggerereignisse (Pulsgenerator an ADQ32-GPIO); 0 = aus |
| `harp.herald2_channel` | 3 |  | Vorschlag | Gesamtplan v1.5, Kanalbelegung | CH3: reserviert für zweiten Herald-Detektor bzw. PNR (nicht simuliert); 0 = keine Reservierung |
| `harp.random_tag_channel` | 4 |  | Vorschlag | Gesamtplan v1.5, Kanalbelegung | CH4 (Hilfskanal): Kopie des Zufallstriggers (Lauf random, Option AP20) |
| `harp.state_tag_channel` | 4 |  | Vorschlag | Gesamtplan v1.5, Kanalbelegung | CH4 (Hilfskanal): Trigger getriggerter Testzustände (nie im selben Lauf wie Zufallstrigger) |
| `harp.marker_timing_channel` | 0 |  | Vorschlag | HydraHarp 500 Manual, Kap. 6.3 (T2) | Timing-Kanal der Sync-Marker, falls markers.on_timing_channel: 0 = Sync-Eingang (in T2 vollwertiger Zeitstempelkanal, Teiler aus) |
| `harp.sync_divider` | 1 |  | **Spez.** | HydraHarp 500 Manual, Kap. 6.3 (T2) | Sync-Teiler; in T2 bei Nutzung des Sync-Eingangs als Kanal 1 ("None") [Spez.] |
| `harp.run_marker_bit` | 1 |  | Vorschlag | Gesamtplan v1.5, Kanalbelegung | Marker-Eingang M1 (Bit 1): Laufmarke bei Laufbeginn und -ende (Quelle [offen]); 0 = aus |
| `harp.d_TC` | 3e-08 | s | Annahme |  | Laufzeit ADQ32-GPIO → Harp CH2 (Kabel, Abschwächer) |
| `harp.channel_offsets` | {} | s | **Spez.** | HydraHarp 500 Manual, Kap. 9.3.1 | Eingangs-Offsets je Kanal {Kanal: s}, Bereich ±100 ns |
| `harp.d_H` | 8.53e-08 | s | Annahme | Vorbericht Synchronisation, Parameter | Laufzeit Herald-Detektor → Harp |
| `harp.d_RH` | 1e-08 | s | Annahme |  | Laufzeit Zufalls-/Zustandstrigger-Generator → Harp |
| `harp.marker_bit` | 4 |  | Vorschlag |  | Marker-Eingang M3 (Bit 4) für Sync-Marker, nur falls nicht auf einem Timing-Kanal (Rückfall); Störmarker-Modell |
| `harp.marker_error_rms` | 3e-08 | s | Annahme | Vorbericht Synchronisation, Parameter | Streuung des Marker-Zeitfehlers (Spez.: Fehler < 100 ns in T2; Gaußmodell = Annahme) |
| `harp.marker_offset` | 2e-08 | s | Annahme | Vorbericht Synchronisation, Parameter | konstante Marker-Latenz |
| `harp.marker_error_max` | 1e-07 | s | **Spez.** | HydraHarp 500 Manual, Kap. 9.3.1 | Grenze des Marker-Zeitfehlers in T2 (Modell wird hier abgeschnitten) |
| `harp.marker_holdoff` | 0 | s | **Spez.** | HydraHarp 500 Manual, Kap. 6.3 | Marker-Hold-off (unterdrückt Folge-Marker) |
| `harp.R_spurious` | 0.2 | 1/s | Annahme | Vorbericht Synchronisation, Parameter | Störmarker (Glitches) an der Harp |
| `digitizer.model` | ADQ32 |  | **Spez.** | ED-6 | Digitizer-Modell |
| `digitizer.form_factor` | PCIe |  | **Spez.** | ADQ32 Datenblatt 20-2378 | Bauform PCIe (ED-14) oder USB |
| `digitizer.clock_option` | S2G5 |  | **Spez.** | ADQ32 Datenblatt 20-2378 | S2G5 (2,5 GS/s je Kanal, 5 GS/s 1-Kanal) oder S2G0 (2/4 GS/s); Festlegung ED-14: S2G5 |
| `digitizer.bandwidth_option` | BW1G0 |  | **Spez.** | ADQ32 Datenblatt 20-2378 | Analogbandbreite BW1G0 (1 GHz) oder BW2G5 (2,5 GHz); Festlegung ED-14: BW1G0 |
| `digitizer.frontend_filter` | first_order |  | Annahme |  | Modell der Analogbandbreite: first_order (Tiefpass 1. Ordnung, −3 dB bei der Bandbreite) oder none |
| `digitizer.channel_mode` | 2 |  | **Spez.** | ADQ32 Datenblatt 20-2378 | 2-Kanal- oder 1-Kanal-Modus |
| `digitizer.skip_factor` | 1 |  | **Spez.** | ADQ3 User Guide 21-2539 | Sample Skip (ohne Tiefpass, User Guide 5.2) |
| `digitizer.record_length` | 5000 | Samples | Vorschlag |  | Recordlänge (2 µs bei 2,5 GS/s; Off-Herald-Fenster) |
| `digitizer.pretrigger` | 2000 | Samples | Vorschlag |  | Pretrigger (negativer horizontal_offset); 2-Kanal: Schritte 8, max 16 360 |
| `digitizer.rearm` | 2e-08 | s | **Spez.** | ADQ32 Datenblatt 20-2378 | Rearm-Zeit (max.) |
| `digitizer.trig_res_ref` | 5e-11 | s | **Spez.** | ADQ32 Datenblatt 20-2378 | TRIG-Zeitauflösung bei 2,5 GS/s (skaliert linear mit der Abtastrate) |
| `digitizer.sync_res_ref` | 3.2e-09 | s | **Spez.** | ADQ32 Datenblatt 20-2378 | SYNC-Zeitauflösung bei 2,5 GS/s (skaliert linear) |
| `digitizer.trig_max_rate` | 1e+07 | 1/s | **Spez.** | ADQ32 Datenblatt 20-2378 | max. Pulsfolgefrequenz am TRIG-Eingang |
| `digitizer.time_unit_div` | 8 |  | Annahme |  | time_unit = 1/(f_ADC·time_unit_div); Wert von time_unit nicht dokumentiert |
| `digitizer.adc_bits` | 12 | bit | **Spez.** | ADQ32 Datenblatt 20-2378 | ADC-Auflösung |
| `digitizer.lsb_per_unit` | 80 | LSB | Annahme | Vorbericht Synchronisation, Parameter | ADC-LSB je √(Vakuumvarianz·2) – Verstärkung |
| `digitizer.adc_offset` | 3 | LSB | Annahme |  | ADC-Nullpunktfehler |
| `digitizer.clearance_dB` | 15 | dB | Annahme | Vorbericht Synchronisation, Parameter | Shot-Noise-Clearance (Vakuum / elektronisches Rauschen) |
| `digitizer.d_T` | 4.27e-08 | s | Annahme | Vorbericht Synchronisation, Parameter | Laufzeit Herald-Detektor → TRIG |
| `digitizer.d_S` | 1.2e-07 | s | Annahme | Vorbericht Synchronisation, Parameter | Laufzeit Modenzentrum → Analogeingang |
| `digitizer.d_RT` | 1.2e-08 | s | Annahme |  | Laufzeit Zufalls-/Zustandstrigger-Generator → TRIG |
| `digitizer.d_MD` | 2.3e-08 | s | Annahme | Vorbericht Synchronisation, Parameter | Laufzeit Markerquelle → SYNC |
| `digitizer.p_miss_marker` | 0.001 |  | Annahme | Vorbericht Synchronisation, Parameter | Anteil verlorener Marker am Digitizer |
| `digitizer.trig_jitter` | 2e-11 | s | Annahme |  | Jitter des TRIG-/SYNC-Eingangs |
| `digitizer.header_reports_trigger_source` | False |  | Annahme |  | meldet der Record-Header den auslösenden Eingang? (unbekannt → false; Kennung über Harp) |
| `digitizer.crosstalk_amp` | 0 | Vakuum-σ | Vorschlag |  | Amplitude eines Trigger-Übersprechens auf den Analogkanal (Fehlerbild R10) |
| `digitizer.crosstalk_tau` | 1.5e-08 | s | Annahme |  | Abklingzeit des Übersprechens |
| `digitizer.trigger_copy_latency` | 1e-08 | s | Annahme |  | Latenz Triggerereignis → Pulsgenerator-Ausgang (GPIO) |
| `digitizer.pulse_gen_granularity` | 32 | Samples | **Spez.** | ADQ3 User Guide 21-2539, Kap. 7.2 (Beispiel ADQ32) | Zeitraster des Pulsgenerators in Samples der Basisabtastrate (5 GHz bei S2G5 → 6,4 ns) |
| `digitizer.crosstalk_delay` | 7.73e-08 | s | Annahme |  | Verzögerung des Übersprechens nach dem TRIG-Puls (Default d_S − d_T: am Modenort, ungünstigster Fall) |
| `clock.eps` | 2e-06 |  | Annahme | Vorbericht Synchronisation, Parameter | relative Frequenzabweichung Digitizer gegen Harp (ADQ32 intern ±3 ppm Spez.; HydraHarp OCXO ±300 ppb) |
| `clock.t_arm_s` | 0.137 | s | Annahme | Vorbericht Synchronisation, Parameter | wahre Zeit, zu der der Digitizer-Zähler startet |
| `clock.rw_D` | 0 | s²/s | Annahme | Vorbericht Synchronisation, Parameter | Diffusionskonstante der Phasen-Irrfahrt (0 = aus) |
| `clock.shared_10MHz` | True |  | Vorschlag | ED-7 | gemeinsame 10-MHz-Referenz (ED-7): ε = 0, keine Irrfahrt |
| `clock.grid_dt` | 0.001 | s | Vorschlag |  | Stützstellenabstand der Irrfahrt |
| `markers.f_marker` | 1000 | Hz | Annahme | Vorbericht Synchronisation, Parameter | Rate der Sync-Marker |
| `markers.p_omit` | 0.1 |  | Annahme | Vorbericht Synchronisation, Parameter | pseudozufällige Auslassung (Muster für die Zuordnung) |
| `markers.on_timing_channel` | True |  | Vorschlag | ED-7; HydraHarp 500 Manual, Kap. 6.3 (T2) | Sync-Marker auf einem Timing-Kanal (Standard: Sync-Eingang, 1 ps) statt auf einem Marker-Eingang (< 100 ns) |
| `markers.sigma_tc` | 5e-12 | s | Annahme | Vorbericht Synchronisation, Parameter | Streuung, wenn der Marker auf einem Timing-Kanal liegt |
| `markers.d_MH` | 1.5e-08 | s | Annahme | Vorbericht Synchronisation, Parameter | Laufzeit Markerquelle → Harp |
| `drift.lo_rel_per_s` | 0 | 1/s | Vorschlag | Gesamtplan v1.4, R15 | lineare relative Drift der LO-Leistung ab Sitzungsbeginn (0 = aus); Vakuumrauschen ∝ √P_LO, elektronisches Rauschen fest |
| `drift.eta_rel_per_s` | 0 | 1/s | Vorschlag | Gesamtplan v1.4, R15 | lineare relative Drift der Zustandseffizienz η je Lauf (Modenanpassung, Justage) |
| `drift.monitor` | False |  | Vorschlag |  | zusätzlich externer, langsamer LO-Leistungsmesser (/blocks/<id>/monitor); Standard ist die Photodiode an ADQ32-Kanal B (lo_monitor) |
| `drift.monitor_rate_Hz` | 10 | Hz | Annahme |  | Abtastrate des externen LO-Leistungsmessers |
| `drift.monitor_noise_rel` | 0.001 |  | Annahme |  | relatives Rauschen des externen LO-Leistungsmessers |
| `lo_monitor.source` | adq32_b |  | Vorschlag | Entscheidung Herr Eilers (v1.5) | adq32_b (Photodiode an Kanal B) oder off |
| `lo_monitor.skip_factor` | 25 |  | Vorschlag | ADQ3 User Guide 21-2539, Kap. 5.2 | Sample Skip für Kanal B (2,5 GS/s → 100 MS/s); Fenster wie Kanal A |
| `lo_monitor.lsb_nominal` | 1000 | LSB | Annahme |  | Signal der Photodiode bei nominaler LO-Leistung über dem Dunkelwert (Eingangsbereich 0,5 Vpp, DC-Offset programmierbar) |
| `lo_monitor.dark_lsb` | 20 | LSB | Annahme |  | Dunkelwert (Photodiode ohne Licht + ADC-Offset); im Labor mit LO aus messen |
| `lo_monitor.noise_lsb` | 3 | LSB | Annahme |  | Rauschen je Sample (Photodiode + Verstärker + ADC) |
| `lo_monitor.rin_rel` | 0.001 |  | Annahme |  | relatives Intensitätsrauschen des LO je Record |
| `blocks[].id` | heralded |  | Vorschlag |  | eindeutiger Blockname |
| `blocks[].kind` | heralded |  | Vorschlag |  | heralded \| random \| free_running \| vacuum \| herald_blocked \| electronic \| state |
| `blocks[].duration_s` | 5 | s | Vorschlag |  | Messdauer des Laufs |
| `blocks[].run_id` |  |  | Vorschlag |  | Lauf-ID; leer = automatisch <session.id>-R<nn> |
| `blocks[].random_rate` | 0 | 1/s | Vorschlag |  | Zufallstrigger (Poisson): Lauftrigger (random) bzw. eingestreut in heralded (Option, ED-12) |
| `blocks[].periodic_rate` | 2000 | 1/s | Vorschlag |  | interne periodische Trigger (vacuum, electronic, free_running) |
| `blocks[].state` | {} |  | Vorschlag |  | Testzustand bei kind=state: {type: coherent\|thermal\|fock\|cat\|vacuum\|fock1, ...} |
| `blocks[].state_rate` | 1000 | 1/s | Vorschlag |  | Rate getriggerter Testzustände (periodisch) |
| `blocks[].phase` | {} |  | Vorschlag |  | blockeigene Überschreibung von phase.* |
| `blocks[].crosstalk_amp` | None |  | Vorschlag |  | blockeigene Überschreibung von digitizer.crosstalk_amp |
| `blocks[].lo_scale` | 1 |  | Vorschlag |  | Faktor auf die LO-Leistung in diesem Lauf (Sprung; zusätzlich zu drift.lo_rel_per_s) |
| `blocks[].eta_scale` | 1 |  | Vorschlag |  | Faktor auf η in diesem Lauf (Sprung; zusätzlich zu drift.eta_rel_per_s) |

## Standard-Messablauf (`blocks`)

| id | kind | duration_s | random_rate | periodic_rate |
|---|---|---|---|---|
| kv_start | vacuum | 0.5 | 0 | 4000 |
| kh | herald_blocked | 1 | 0 | 2000 |
| heralded_1 | heralded | 2.5 | 0 | 2000 |
| unheralded | free_running | 1 | 0 | 2000 |
| heralded_2 | heralded | 2.5 | 0 | 2000 |
| kv_end | vacuum | 0.5 | 0 | 4000 |
