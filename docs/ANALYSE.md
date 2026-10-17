# Auswertung: Normierung, Moden, Rekonstruktion, Vergleich A/B

Stand 0.9.0.dev0 – Python/NumPy-Referenzimplementierung (Gesamtplan v1.7, Arbeitspakete Normierung/Kalibrierung,
Modenextraktion, Rekonstruktion, Vergleich A/B). Eingang ist die Synchronisation (`sync/sync.h5`, `sync/sync.json`,
siehe `docs/SYNC.md`); fehlt sie, wird sie erzeugt. **Die Auswertung liest die Wahrheitsdatei nie**
(`truth.h5` ist im Import gesperrt; ein Test prüft die Quellen). Abgleich mit der Wahrheit nur im
Validierungswerkzeug `hhd.sim.analysis_validate` bzw. `hhd validate-analysis`.

## 1 Kommandos

```bash
hhd calibrate   data/S001        # Quadraturen, Vakuum-/Elektronik-/LO-Kalibrierung, Drift, K-H-Übersprechen, PCA
hhd reconstruct data/S001        # zusätzlich Rekonstruktion je Gruppe mit Bootstrap
hhd compare     data/S001        # vollständige Kette inkl. Vergleich A/B → data/S001/analysis/
hhd report      data/S001        # Bericht (Markdown) aus analysis/result.json
hhd validate-analysis data/S001  # nur Simulation: Pulls gegen truth.h5
```

Wichtige Optionen: `--norm pooled|interpolated|monitor|monitor_record`, `--clearance-db 15` (falls kein Dunkellauf),
`--estimator maxlik|em|binned|moments`, `--nmax 10`, `--n-boot 200`, `--mode-source parametric|pca|pca_raw`,
`--no-pca`, `--no-bandwidth`, `--no-boot-vacuum` (alte Fehlerrechnung, nur zum Vergleich), `--dark-count-rate 100`,
`-o DIR`. Kampagnen: Pfad des Kampagnenordners, je Sitzung `analysis/`.

Ausgabe in `<sitzung>/analysis/`:

| Datei | Inhalt |
|---|---|
| `result.json` | Schema `hhd-result/1.0` (Abschnitt 7) |
| `quadratures.h5` | je Lauf `q_raw_lsb`, normierte `x`, `coverage`, `clean`, `seg_class`, `t_session_s`, `lo_b_lsb`; `groups/<Gruppe>/<Lauf>` Masken |
| `report.md` | Kurzbericht |

## 2 Modenextraktion (`hhd/modes`)

* **Modenzentrum je Record**: zugeordnete Herald-Records Herald-Zeit (Digitizer-Uhr) + Δ des Laufs, alle anderen
  Trigger + Δ_T (aus `sync.json`).
* **Quadratur** q = Σ f(t_k − t_c)·(y_k − y₀) mit normierter Modenfunktion (Σ f² = 1) aus den Parametern
  (`double_exponential` bzw. `gauss`, γ = 1/40 ns). Unterabtastgenaue Lage über eine Vorlagenbank (32 Bruchteile je
  Sample). y₀ = ADC-Nullpunkt aus K-V-Records.
* **Bandbreitenmodell**: Vorlage mit dem Analogfilter des Digitizers (1. Ordnung, `analog_bandwidth_Hz`) gefaltet;
  Effekt für die 40-ns-Mode 2,7·10⁻⁶ (ausgewiesen, `mode.bandwidth_effect`).
* **Sauber** = zugeordnet, keine sekundäre Mode, keine Überlappung, Abdeckung der Vorlage im Record ≥ 0,99.
* **Datengetriebene Mode** (`hhd/modes/pca.py`): Fenster ±320 ns, Bins zu 8 Samples, ΔC = C_A − C_KV; erster
  Eigenvektor ≙ Mode, λ₁ ≙ ⟨x²⟩ − ½. Ausgewiesen: Überlapp |⟨e₁,f⟩|² mit der parametrischen Mode, **erwarteter
  Rauschanteil** des Überlapps (Störungsrechnung, Wishart-Näherung) und rauschkorrigierter Überlapp, λ₂/λ₁,
  angepasstes τ und Zentrumsversatz. Kampagne (64 Sitzungen): Überlapp roh 0,928, Rauschanteil erwartet 0,065,
  korrigiert 0,993 ± 0,006; τ_fit = 39,9 ns (Soll 40), Versatz +0,25 ns. Die Wahrheit zeigt einen gemeinsamen
  Zentrumsfehler von −0,82 ns (Δ-Schätzung) bei 17 ps Streuung; daraus folgt ein Überlappverlust von nur 4·10⁻⁴ –
  der Rest bis 0,993 ist Ungenauigkeit der Rauschkorrektur (Näherung erster Ordnung). Der PCA-Überlapp ist daher eine
  **Prüfung** (grobe Fehlanpassung, Mehrmodigkeit), kein Präzisionswert für das Effizienzbudget.
* **Option** `--mode-source pca`: parametrische Mode mit τ und Versatz aus der PCA-Anpassung (2 Parameter, keine
  nennenswerte Auswahlverzerrung). `pca_raw` (rohe Eigenmode) nur zur Prüfung: an denselben Daten bestimmt, Varianz
  um ~0,1 zu groß (Warnung).

## 3 Normierung und Kalibrierung (`hhd/calib`)

Modell: q = s_v·√(P/P_v)·x + e mit Var(e) = σ_el². K-V: Var(q_KV) = s_v²·½ + σ_el². Normiert wird mit der
Vakuumvarianz der K-V-Läufe, x = (q − q₀)·√(½/Var_v); elektronisches Rauschen wirkt so als Verlust
η_el = 1 − σ_el²/Var_KV.

| Normierung | Vakuumvarianz Var_v |
|---|---|
| `pooled` (Standard) | gemeinsames K-V-Mittel der Sitzung |
| `interpolated` | zeitlich linear zwischen den K-V-Läufen |
| `monitor` | Var_KV·[(1−f_el)·P_Lauf/P_KV + f_el], P aus Kanal B je Lauf |
| `monitor_record` | wie `monitor`, je Record mit dem Kanal-B-Mittel |

* **Elektronisches Rauschen / Clearance**: aus Läufen `electronic` (LO aus), sonst `--clearance-db`, sonst Warnung
  (η_el fehlt im Budget). Test mit Dunkellauf: η_el = 0,969 (Soll 0,9693).
* **LO-Monitor (Kanal B)**: Dunkelwert aus `electronic`-Läufen, sonst nominell aus `lo_monitor_dark_lsb` (Warnung);
  P/P_KV je Lauf und je Record.
* **Driftindikatoren** (Warnung ab |z| > 3): K-V erster ↔ letzter Lauf und gewichteter Trend (χ²), LO-Monitor
  (max. Abweichung, Schwelle 1 %), **A-B-A** (A-Varianz vor/nach jedem B), **A erste ↔ zweite Hälfte**,
  χ² der Laufvarianzen von A und B. Nullverteilung in 64 Sitzungen ohne Drift: K-V z 0,01 ± 1,10,
  A-Hälften z −0,02 ± 1,05, A-B-A χ² 6,0 bei 6 Freiheitsgraden – die Schwelle 3 ist kalibriert (OP-6:
  Fehlalarme 1/64 für A-Hälften).
* **K-H-Übersprechen**: Modenquadratur der K-H-Records (⟨x⟩, Var x gegen ½, Fehler inkl. Vakuumnormierung) und
  Wellenform K-H − K-V in Bins zu 4 ns (Fehler aus der Streuung der Bin-Mittel je Record, da benachbarte Samples
  korreliert sind), Warnung ab max |z| > 5. Test mit `crosstalk_amp = 0,3` erkannt. Nullverteilung (64 Sitzungen,
  500 Bins): max |z| 3,30 im Mittel, höchstens 4,10 – kein Fehlalarm; K-H-Varianz z mit Streuung 1,16 (1 Warnung
  bei |z| = 3,2).

## 4 Rekonstruktion (`hhd/recon`, phasengemittelt, Standard v1.0)

* Schätzer: **MaxLik** (iterativ RρR, Standard), **EM**, `binned` (RρR auf Histogramm, ab 50 000 Records je Gruppe
  automatisch, gleiches Ergebnis ~100× schneller), `moments` (erwartungstreu für ρ_nn = 0, n > N; Prüfgröße).
  Fock-Abschneidung `nmax` (Standard 10), Konvergenz über Δp und Δlog L, Warmstart im Bootstrap.
* Ausgaben je Gruppe (A, B, eingestreute Zufallstrigger, K-H, Zustandstrigger): p_n, ⟨n⟩, g⁽²⁾, Mandel-Q, W(0,0),
  radiale Wigner-Funktion W(r), Var x, η-Fit im Modell (1−η)|0⟩⟨0| + η|1⟩⟨1|, Fidelity zum verlustbehafteten |1⟩
  (Diagonal) und zu |1⟩, W(0,0) aus Momenten mit analytischem Fehler.
* **Bootstrap** (Standard 200): je Ziehung **neue Vakuumkalibrierung aus den K-V-Records** (geschichtet je K-V-Lauf)
  **und** neue Records je Gruppe; dieselbe Vakuumziehung für A und B (Korrelation im Vergleich korrekt).
  Fehler, 68-/95-%-Intervalle, Bootstrap-Bias und biaskorrigiertes `W00_bc`. Ab 0.8 sechs Intervallmethoden
  (`hhd/recon/intervals.py`, alle in `ci_all`), Standard **normal** (θ̂ ± z·sd(θ*)), siehe Abschnitt 11.
* **θ-Option** (`hhd/recon/phase.py`): nur Schnittstelle (`phase_fit`; `reconstruct_with_phase` meldet
  `NotImplementedError` bis ED-11).

### OP-1: η-Fehler um Faktor ~1,4 unterschätzt – gelöst

Ursache: Die Unsicherheit der Vakuumnormierung (relativ √(2/N_KV) ≈ 1,7 % bei 6 600 K-V-Records) fehlte im
Bootstrap. Lösung: gemeinsamer Bootstrap von K-V und Signal. Quadraturstudie (`tools/op1_vacuum_bootstrap.py`,
N_A = 5 000, 300 Wiederholungen, Pull-Breite ± 0,04):

| K-V-Records | η-Fit alt | η-Fit gemeinsam | Var − ½ alt | Var − ½ gemeinsam |
|---|---|---|---|---|
| 2 000 | 1,64 | 0,98 | 2,53 | 1,04 |
| 6 600 | **1,29** | **1,04** | **1,61** | **1,04** |
| 20 000 | 1,12 | 1,03 | 1,26 | 1,04 |
| 50 000 | 1,08 | 1,04 | 1,11 | 1,02 |

Vollständige Kette, 64 Sitzungen: Pull-Breite η-Fit 1,32 → **1,06 ± 0,09**, Var x (A) 1,53 → **0,99 ± 0,09**,
Var x (B) 1,28 → 1,01, η aus dem Vergleich 1,18 → 1,04. Kriterium 1 ± 0,1 erfüllt.

### OP-2: W(0,0)-Bias +0,006 – Ursache gefunden, korrigiert und ausgewiesen

Quadraturstudie (`tools/op2_w00_bias.py`, verlustbehaftetes |1⟩ mit η_eff = 0,617, W_wahr = −0,0744):

| Schätzer | N = 1 000 | N = 5 000 |
|---|---|---|
| EM 150 Iterationen (alter Bootstrap) | +0,0064 (0,34 σ) | +0,0030 (0,37 σ) |
| EM 400 Iterationen (alte Referenz) | +0,0058 | +0,0020 |
| EM / MaxLik konvergiert, nmax 6/10/16 | +0,0056 (0,30 σ) | +0,0018 (0,22 σ) |
| MaxLik Histogramm 200 / 40 Bins | +0,0056 / +0,0051 | +0,0018 / +0,0017 |
| Momente N = 2 / 3 | −0,0016 / +0,0009 (± 0,002) | +0,0001 / −0,0011 (± 0,001) |
| Modellfit n ≤ 1 | +0,0009 (± 0,001) | −0,0007 (± 0,0005) |
| MaxLik mit Bootstrap-Biaskorrektur | +0,0038 (0,19 σ) | +0,0007 (0,08 σ) |

Ergebnis: Der Bias ist eine Eigenschaft des **positivitätsbeschränkten** ML-Schätzers (p_n ≥ 0 am Rand für n ≥ 2;
Rauschen kann nur positive Gewichte bei hohen n erzeugen) und fällt etwa wie 1/N. **Nicht** die Ursache:
Fock-Abschneidung (nmax 6 … 16 gleich), Histogramm-Binning (gleich). Unvollständige EM-Konvergenz (150 Iterationen)
erhöhte ihn um weitere +0,001. Der frühere Wert +0,006 entspricht N ≈ 1 000 je Gruppe.
Maßnahmen: konvergierter Schätzer, `W00_bc` (Bootstrap-Biaskorrektur, halbiert den Bias, < 0,2 σ ab N ≈ 1 000)
und der erwartungstreue Momentenschätzer als Gegenprobe werden in `result.json` ausgewiesen.
Vollständige Kette (64 Sitzungen, N_A ≈ 5 000): W00 +0,0045 ± 0,0012 (0,47 σ), W00_bc +0,0036 ± 0,0012,
Momente +0,0048 ± 0,0019. Der gemeinsame Anteil von ~+0,003 gehört nicht zum Schätzer, sondern ist eine
Schwankung der erzeugten Stichproben: Gepaarter Vergleich mit den Mode-Quadraturen der Wahrheit (nur Validierung):
Var_gemessen − Var_erwartet(Stichprobe) = −0,0028 ± 0,0028, d. h. **keine Systematik der Analysekette**; die
erzeugten Stichproben selbst liegen um −0,0029 unter dem Sollwert.

## 5 Vergleich A/B (getrennte Läufe)

Gemeinsame Kalibrierung, Histogramme (60 Bins, ±5) mit Poisson-Fehlern und χ², **KS-Test** (`scipy.stats.ks_2samp`),
Differenzen von Var x, W(0,0) (MaxLik und Momente) und η-Fit mit Fehlern aus dem gemeinsamen Bootstrap,
Differenz der radialen Wigner-Funktion. **Effizienz aus dem Vergleich**: η = Var_A − Var_B
(B als Referenz statt ½; unempfindlich gegen gemeinsame Normierungsfehler). Kampagne: Pull-Breite 1,04 ± 0,09,
Bias −0,0055 ± 0,0029.

## 6 Effizienzbudget

η_eff (Fit, Var − ½, Vergleich) = ξ · η_el · η_rest mit ξ = 1 − R_dunkel/R_Herald (gemessene Herald-Rate,
`--dark-count-rate`), η_el aus der Kalibrierung (falls bekannt), Modenüberlapp der PCA zur Information.
Kampagne: ξ = 0,9092 (Soll 0,9091), η_rest Pull −0,20 ± 0,13, Breite 1,07.

## 7 Ergebnisschema `hhd-result/1.0` (`result.json`)

`schema`, `software`, `session`, `session_id`, `campaign_id`, `calibration_id`, `params` (alle Analyseparameter),
`mode` (Form, γ, fs, Bandbreite, Effekt, ADC-Nullpunkt), `calibration` (Normierung, q₀, Var_KV, je K-V-Lauf,
Elektronik mit Quelle, η_el, Clearance, Dunkelwert Kanal B mit Quelle, LO je Lauf, Skalen je Lauf, Warnungen),
`drift_vacuum_lo`, `crosstalk`, `pca`, `groups` (Läufe, Records), `reconstruction` (je Gruppe Kenngrößen,
`*_err`, `*_boot` mit `ci68`/`ci95`/`bias_boot`, `W00_bc`, `p_n`, `wigner_r`/`wigner_radial`), `drift_AB`
(je Lauf, A-B-A, A-Hälften, χ²), `comparison` (Histogramme, KS, Differenzen, Wigner-Differenz,
`eta_from_comparison`), `efficiency_budget`, `warnings`, `performance`. Nicht endliche Zahlen als `null`.

## 8 Validierung und Kampagnenstudie

`tools/campaign_analysis.py <kampagne[,…]> [n_boot] [n_proc]`: je Sitzung Auswertung (gemeinsamer Bootstrap) und
Vergleich ohne Vakuumbootstrap, Validierung, Zusammenfassung (Pull-Mittel/-Breite, Bias, Abdeckung).
64 Sitzungen Standardplan (Seeds 500–523, 600–619, 700–719), n_boot = 200:

| Größe | Pull-Mittel | Pull-Breite | Bias | Abdeckung 68 % / 95 % |
|---|---|---|---|---|
| A Var x | −0,21 ± 0,12 | 0,99 ± 0,09 | −0,0048 ± 0,0031 | 66 % / 97 % |
| A W(0,0) MaxLik | +0,46 ± 0,12 | 0,97 ± 0,09 | +0,0045 ± 0,0012 | 58 % / 91 % |
| A W(0,0) biaskorr. | +0,36 ± 0,13 | 1,02 ± 0,09 | +0,0036 ± 0,0012 | – |
| A W(0,0) Momente | +0,27 ± 0,12 | 0,99 ± 0,09 | +0,0048 ± 0,0019 | 64 % / 94 % |
| A η-Fit | −0,20 ± 0,13 | 1,06 ± 0,09 | −0,0032 ± 0,0020 | 59 % / 92 % |
| B Var x | +0,02 ± 0,13 | 1,01 ± 0,09 | +0,0007 ± 0,0018 | 64 % / 95 % |
| B W(0,0) Momente | −0,07 ± 0,12 | 0,98 ± 0,09 | −0,0009 ± 0,0015 | 70 % / 95 % |
| η aus Vergleich | −0,26 ± 0,13 | 1,04 ± 0,09 | −0,0055 ± 0,0029 | – |
| W_A − W_B | +0,70 ± 0,11 | 0,91 ± 0,08 | +0,0076 ± 0,0013 | – |
| η_rest (Budget) | −0,20 ± 0,13 | 1,07 ± 0,09 | −0,0035 ± 0,0022 | – |

B W(0,0) aus MaxLik liegt für Vakuum am Rand (W = 1/π) und hat keinen sinnvollen Pull; dafür die Momente.
W_A − W_B enthält zusätzlich die Randverzerrung von B (MaxLik ≤ 1/π).

## 9 Leistung (Box, 8 Kerne, je Prozess 1–2 Threads)

| Fall | Records | Laufzeit |
|---|---|---|
| Standardsitzung (17 Läufe, 0.7), n_boot 200 | 16 500 | 4,9 s (Extraktion 1,8 s, PCA 1,7 s, Bootstrap 0,5 s) |
| Stresssitzung 1 MHz (246 MB), n_boot 200 | 159 000 (120 000 in A) | 10,4 s, 290 MB Speicher |
| Kampagne 64 Sitzungen, 6 Prozesse (je Sitzung 2 Auswertungen) | 1,6 Mio. | 239 s (11 s je Sitzung) |

## 10 Grenzen und offene Punkte

* Bei hohen Herald-Raten (Stresssitzung 1 MHz) enthält A zusätzlich ungeheraldete Photonen; der Sollwert der
  Wahrheit schließt sie aus (Hinweis im Sollwert), daher dort große Pulls (W00 +9,9) – physikalisch erwartet, über
  den Vergleich A/B sichtbar.
* PCA-Rauschkorrektur nur erster Ordnung (Abschnitt 2).
* θ-Rekonstruktion nur Schnittstelle (ED-11).
* MaxLik-Bias +0,0018 bei N = 5 000 bleibt (0,2 σ), biaskorrigiert 0,08 σ; Standardschätzer bleibt MaxLik.
* Driftindikatoren haben in Zeitraffer-Sitzungen (1/90 der M1-Daten) kaum Trennschärfe; Schwellen und
  Erkennungsraten für Echtzeit sind hochgerechnet bzw. mit 10-fachen Sitzungen geprüft (Abschnitt 11.4).

## 11 Version 0.8: Konfidenzintervalle, W(0,0)-Rest, Dunkellauf, OP-5, OP-6

Studien: `docs/studien/kampagne200_ki_op5.json` (200 neue Sitzungen + Neuauswertung der 64 Sitzungen aus 0.7),
`docs/studien/op6_driftschwellen_zeitraffer.json`, `docs/studien/op6_driftschwellen_10x.json`.
Werkzeuge: `tools/campaign_analysis.py` (Abdeckung je Methode, Driftindikatoren je Sitzung, `--fast`),
`tools/drift_thresholds.py` (Fehlalarm, Erkennung, Hochrechnung).

### 11.1 Abdeckung der Konfidenzintervalle (η-Fit, W(0,0))

Ausgangslage 0.7 (64 Sitzungen): 68-%-Abdeckung η-Fit 59 %, W(0,0) 58 % (95 %: 92 %/91 %) bei Pull-Breiten ≈ 1.
Untersucht wurden die Ursachen aus dem Auftrag:

| mögliche Ursache | Befund |
|---|---|
| Bootstrap-Varianz am Positivitätsrand unterschätzt | nein: Pull-Breite W(0,0) 0,95 ± 0,05, η-Fit 0,99 ± 0,05 (200 Sitzungen); mittlerer Bootstrap-Fehler = Streuung (0,0096 vs. 0,0091) |
| Perzentil vs. BCa vs. studentisiert | sechs Methoden implementiert und verglichen (Tabelle); BCa/Perzentil/normal gleichwertig, studentisiert überdeckt (Var x 87 %/99,6 %: Fehler je Ziehung korreliert mit θ*) |
| Bias im Intervall | W(0,0): +0,28 σ Pull-Mittel kostet ≈ 1,5 Prozentpunkte bei 68 %; biaskorrigierte Intervalle (`bc_normal`) ändern die Abdeckung nicht messbar |
| Kalibrierungs-/Modenunsicherheit | Vakuumnormierung im Bootstrap (OP-1); η_el-Fehler neu im Budget; Modenzentrum (17 ps Streuung) und PCA-Überlapp vernachlässigbar |
| Abhängigkeit innerhalb der Sitzung | gemeinsame Vakuumziehung für alle Gruppen; A-A-/B-B-Nulltests gleichverteilt (11.3) – keine Laufabhängigkeit sichtbar |
| Statistik der 64 Sitzungen | **Hauptursache**: Binomialfehler bei 64 Sitzungen ± 5,8 Punkte (68 %); dieselben 64 Sitzungen mit Code 0.8: 56–59 %, die 200 neuen Sitzungen: 71–72 % |

Abdeckung (Anteil der Sitzungen, deren Intervall den wahren Wert enthält), kombiniert 264 Sitzungen
(Binomialfehler ± 2,9 / ± 1,3 Punkte):

| Größe | normal (Standard) | BCa | Perzentil | basic | studentisiert |
|---|---|---|---|---|---|
| A W(0,0) MaxLik | **68,2 / 94,7 %** | 68,6 / 93,2 % | 65,9 / 93,2 % | 67,4 / 93,9 % | – |
| A η-Fit | **68,2 / 93,6 %** | 68,2 / 92,0 % | 68,6 / 93,2 % | 68,6 / 92,8 % | 77,3 / 97,7 % |
| A Var x | **67,0 / 96,6 %** | 67,0 / 94,7 % | 65,5 / 95,5 % | 67,0 / 95,5 % | 87,1 / 99,6 % |
| A W(0,0) Momente | **64,4 / 95,8 %** | 64,0 / 94,3 % | 64,4 / 95,1 % | 62,9 / 94,3 % | 61,0 / 95,8 % |
| B Var x | **65,2 / 92,8 %** | 62,9 / 92,0 % | 65,2 / 93,2 % | 64,4 / 91,7 % | 76,5 / 98,1 % |

Nur die 200 neuen Sitzungen (± 3,3 / ± 1,5): W(0,0) normal 72,0/95,5 %, BCa 72,0/94,0 %; η-Fit normal 71,5/94,5 %,
BCa 71,0/93,0 %. **Ergebnis:** Die Intervalle erreichen 68/95 % im Rahmen der Statistik. Standard ab 0.8:
`normal` für alle Größen (`CI_DEFAULT`), alle Methoden zusätzlich in `ci_all`; Feldnamen `{größe}_ci68`,
`{größe}_ci95`, `{größe}_ci_method`, `{größe}_ci_params` (z₀, a, Bias, sd). B Var x liegt mit Pull-Breite 1,06 leicht
unter Soll (Restpunkt).

### 11.2 W(0,0)-Rest

200 Sitzungen (N_A ≈ 5 100): W(0,0) MaxLik **+0,0028 ± 0,0006** (Pull-Mittel +0,28 ± 0,07, Bias = 0,29 σ_stat),
biaskorrigiert `W00_bc` +0,0017 ± 0,0007 (Pull +0,16), Momente −0,0000 ± 0,0011. Erwartung aus OP-2 für den
positivitätsbeschränkten Schätzer bei N = 5 000: +0,0018. Rest nach Abzug: +0,0010 ± 0,0006 (1,7 σ, 0,1 σ_stat).
Die Momente (erwartungstreu) zeigen keinen Bias; der in 0.7 gemeinsame Anteil von +0,003 war eine
Stichprobenschwankung (bestätigt den gepaarten Wahrheitsvergleich aus 0.7). **Ergebnis:** Der W(0,0)-Rest ist
vollständig durch den Positivitätsrand des ML-Schätzers erklärt; `W00_bc` und `W00_moments` bleiben ausgewiesen.

### 11.3 OP-5: Nulltests A-A und B-B

Je Sitzung werden die A- bzw. B-Läufe abwechselnd in zwei Hälften geteilt (`comparison.null_tests`: KS-p, ΔVar, z).
200 Sitzungen ohne Drift:

| Test | KS gegen Gleichverteilung | Anteil p < 0,05 | Anteil p < 0,01 |
|---|---|---|---|
| A-A KS | p = 0,50 | 6,5 % | 2,5 % |
| B-B KS | p = 0,65 | 6,5 % | 2,5 % |
| χ² der A-Läufe | p = 0,69 | 4,5 % | 1,0 % |
| χ² der B-Läufe | p = 0,19 | 3,0 % | 0,5 % |
| A-B-A χ² | p = 0,62 | 8,5 % | 2,0 % |
| **A-B KS** | – | **100 %** (Median p = 3·10⁻⁴⁹) | 100 % |

A-A/B-B-Varianz-z: Mittel +0,16 ± 0,07 bzw. +0,03 ± 0,07, Breite 0,99 bzw. 1,01. **Ergebnis:** Die p-Werte der
Nulltests sind gleichverteilt, A-B wird in jeder Sitzung getrennt. Warnung ab 0.8, wenn |z| > `drift_z` oder
KS-p < `drift_p`.

### 11.4 OP-6: Driftschwellen

Driftkampagnen (je 25 Sitzungen, Zeitraffer): LO +2 % linear über die Sitzung (`lo_lin2`), LO-Sprung +2 % ab a_04
(`lo_step2`), η −5 % linear (`eta_lin5`), η −10 % nur in a_04 (`eta_step10`); Nullkampagne = 200 Sitzungen aus 11.1.
Schwelle je Indikator für Fehlalarm α = 0,27 % (3σ):

| Indikator | Regel | Fehlalarm (200) | LO lin 2 % | LO Sprung 2 % | η lin −5 % | η Sprung −10 % |
|---|---|---|---|---|---|---|
| LO-Monitor `lo_dev` | \|ΔP/P\| > 0,5 % (Standard) | 0 % (Null max 0,016 %) | **100 %** | **100 %** | 0 % | 0 % |
| K-V-Varianz `kv_z`, Steigung | \|z\| > 3 | 0,5 % | 4 % / 0 % | 0 % | 0 % | 0 % |
| A-Hälften `halves_z` | \|z\| > 3 | 0 % | 0 % | 0 % | 0 % | 0 % |
| χ² A-Läufe | p < 0,0027 | 0 % | 0 % | 0 % | 0 % | 0 % |
| A-B-A max \|z\| | > 3,5 | 0 % | 0 % | 0 % | 0 % | 0 % |
| beliebiger Indikator | – | **3,0 %** | 100 % | 100 % | 16 % | 8 % |

In Zeitraffer-Sitzungen (je A-Lauf ≈ 670 Records, σ_Var ≈ 6 %) sind η-Änderungen von 5–10 % statistisch nicht
nachweisbar; LO-Drifts erkennt der Monitor sicher. Hochrechnung auf den M1-Plan in Echtzeit (90-fache Datenmenge,
z ∝ √N; Nichtzentralität der χ²-Tests ×90; Fehler der Hochrechnung etwa ±50 % wegen 25 Sitzungen je Szenario):
LO lin 2 % über K-V 98 %, LO-Sprung 2 % über A-Hälften 100 %, η lin −5 % über A-Hälften 96 %, η-Sprung −10 %
über χ² der A-Läufe ≈ 100 %. Prüfung mit 10-fachen Sitzungen siehe 11.5.

**Standards ab 0.8:** `CalibParams.drift_z = 3,0`, `CalibParams.lo_rel_warn = 0,005` (vorher 0,01; erkennt 2-%-Drifts,
toleriert die erwartete LO-Irrfahrt von 0,5 %/√h über eine 12-min-Sitzung), `crosstalk_z = 5,0` (Null: max 4,35 in
200 Sitzungen), `AnalysisParams.drift_z = 3,0`, neu `AnalysisParams.drift_p = 0,0027` (χ² der A-/B-Läufe, A-B-A-χ²,
Nulltests). A-B-A max |z| > drift_z + 0,5 (Bonferroni über 6 Paare).

### 11.5 Prüfung mit 10-facher Datenmenge

Je 7 Sitzungen ohne Drift (Messung A/B, `null_a`, `null_b`), 7 mit η-Sprung −10 % und 6 mit η linear −5 %
(`eta_lin5_a/_b`), jeweils mit 10-facher Datenmenge gegenüber dem Zeitraffer (`tools/drift_thresholds.py … 0.0027 9`,
Hochrechnung Faktor 9 auf Echtzeit; `docs/studien/op6_driftschwellen_10x.json`):

| Indikator | Fehlalarm (14) | η-Sprung −10 % | η lin −5 % | Hochrechnung Echtzeit |
|---|---|---|---|---|
| A-B-A-χ² p < 0,0027 | 0/14 | 5/7 | 0/6 | η-Sprung 100 % |
| χ² A-Läufe p < 0,0027 | 0/14 | 5/7 | 0/6 | η-Sprung 100 %, η lin 20–46 % |
| A-B-A max \|z\| > 3,51 | 0/14 | 4/7 | 0/6 | – |
| AA-Var z, AA-KS | 0/14 | 3/7 | 0/6 | – |
| K-V z (η lin) | 0/14 | 0/7 | 0/6 | η lin 2–87 % |
| A-Hälften z (η lin) | 0/14 | 0/7 | 0/6 | η lin 26–29 % (mittleres z −0,80) |
| beliebiger Indikator | **2/14** (kh_z_mean, BB-KS) | **5/7 (71 %)** | 0/6 | – |

Ergebnis: Der η-Sprung −10 % wird bereits mit 10-facher Datenmenge in 71 % der Sitzungen erkannt (Zeitraffer 8 %),
die Hochrechnung „≈ 100 % in Echtzeit" über χ² der A-Läufe / A-B-A-χ² ist bestätigt. Für η linear −5 % ist die
Echtzeit-Hochrechnung aus dem Zeitraffer (96 % über A-Hälften) **zu optimistisch**: aus den 10-fachen Sitzungen folgen
nur etwa 30 % über A-Hälften bzw. bis 87 % über K-V z (große Streuung zwischen den zwei Teilkampagnen, je 3 Sitzungen).
Eine langsame η-Drift von 5 % je Sitzung ist damit in Echtzeit nur teilweise nachweisbar; sicher erkannt werden
Drifts ab etwa 10 %. Fehlalarm je Sitzung 2/14 = 14 ± 9 % (Zeitraffer 3,0 %; bei 14 Sitzungen nicht signifikant
verschieden, Einzelindikatoren je 0–1/14). Die Standardschwellen aus 11.4 bleiben unverändert; eine Prüfung in
Echtzeit (90-fach, ca. 10 GB je Sitzung) steht aus.

### 11.6 Dunkellauf und η_el

Der M1-Laufplan enthält nach dem ersten K-V einen **Dunkellauf** (LO aus, 10 s, 10 kHz periodisch;
`PlanSpec.T_dark`, `rate_dark`, optional `dark_end`), damit 18 Läufe. Er liefert σ²_el und η_el = 1 − σ²_el/Var_KV mit
Fehler f_el·√(2/N_el + 2/N_KV) (`Calibration.eta_el_err`), der ins Effizienzbudget (`eta_el_err`, Fehler von
`eta_rest`) eingeht. Simulator: `kind: electronic`; Laufplanstatistik `sigma_var_el_rel`. 200 Sitzungen:
η_el = 0,96929 ± 0,00153 (Streuung), wahr 0,96935, mittlerer Fehler 0,00141, Pull −0,01 ± 0,08 (Mittel),
Breite 1,11. η_Zustand (η_eff/(ξ·η_el)) Pull +0,16 ± 0,07, Breite 1,01.
