"""Kurzbericht (Markdown, deutsch) aus ``result.json``."""
from __future__ import annotations

import json
from pathlib import Path


def _f(v, fmt="{:.4f}"):
    return "–" if v is None else fmt.format(v)


def _pm(g, q, fmt="{:.4f}"):
    v = g.get(q); e = g.get(f"{q}_err")
    return f"{_f(v, fmt)} ± {_f(e, fmt)}" if e is not None else _f(v, fmt)


def render_report(r: dict) -> str:
    L = [f"# Auswertung {r.get('session_id', '')}", "",
         f"Sitzung `{r['session']}`, Software hhd {r['software']['version']}, Schema `{r['schema']}`.", ""]
    st = r.get("state") or {}
    if st:
        L += [f"Geheraldeter Zustand (A): **{st.get('label', st.get('preset'))}** (`{st.get('preset')}`"
              + (f", ⟨n⟩ ideal {st['mean_n_ideal']:.3f}" if st.get("mean_n_ideal") is not None else "") + ")"
              + ("; phasenempfindlich – Auswertung und Sollwerte nur phasengemittelt" if st.get("phase_sensitive") else "")
              + ("; Annahme (keine Angabe in den Daten)" if st.get("assumed") else "") + ".", ""]
    c = r["calibration"]; p = r["params"]
    L += ["## Kalibrierung", "",
          f"* Normierung: **{c['norm']}**, K-V-Records: {c['n_kv']}, Skala {c['scale_lsb']:.2f} LSB/√(½)",
          f"* Elektronisches Rauschen: {c['el_source']}; η_el = {_f(c.get('eta_el'))}, Clearance "
          f"{_f(c.get('clearance_dB'), '{:.2f}')} dB",
          f"* LO-Monitor: Dunkelwert {_f(c.get('dark_b'), '{:.1f}')} LSB ({c['dark_b_source']})"]
    dv = r.get("drift_vacuum_lo", {})
    if "vacuum" in dv:
        v = dv["vacuum"]
        L.append(f"* K-V-Drift {v['first']} → {v['last']}: {v['var_rel_change']:+.2%} (z = {v['z']:+.2f}), "
                 f"Trend χ²/ndof = {v['chi2']:.1f}/{v['ndof']}")
    if "lo_monitor" in dv:
        L.append(f"* LO-Monitor: max. Abweichung {dv['lo_monitor']['max_rel_dev']:+.3%} ({dv['lo_monitor']['worst']})")
    xt = r.get("crosstalk", {})
    if xt.get("available"):
        w = xt["waveform"]
        L.append(f"* K-H-Übersprechen: z_mean = {xt['z_mean']:+.2f}, z_var = {xt['z_var']:+.2f}, "
                 f"Wellenform max |z| = {w['max_abs_z']:.1f} ({w['n_bins']} Bins)")
    m = r["mode"]
    L += ["", "## Mode", "", f"* {m['shape']}, τ = {1e9 / m['gamma']:.1f} ns, Bandbreitenmodell "
          f"{_f(m.get('bandwidth_Hz'), '{:.2e}')} Hz (Effekt {m['bandwidth_effect']:.1e})"]
    if r.get("pca"):
        q = r["pca"]
        L.append(f"* PCA (C_A − C_KV): λ₁ = {q['lambda1']:.3f}, λ₂/λ₁ = {q['ratio_l2_l1']:.3f}, Überlapp mit "
                 f"parametrischer Mode {q['overlap_param']:.4f} (Rauschanteil erwartet {q['overlap_noise_expected']:.4f}, "
                 f"korrigiert {q['overlap_param_corrected']:.4f}), τ_fit = {q['tau_fit_ns']:.1f} ns, "
                 f"Zentrumsversatz {q['centre_shift_ns']:+.1f} ns")
    rec = r.get("reconstruction", {})
    if rec:
        L += ["", f"## Rekonstruktion (phasengemittelt, {p['estimator']}, n ≤ {p['nmax']}, Bootstrap {p['n_boot']}"
              f"{', mit Vakuumnormierung' if p['boot_vacuum'] else ''})", "",
              "| Gruppe | N | Var x | W(0,0) | W(0,0) biaskorr. | W(0,0) Momente | η (Fit) | ⟨n⟩ | F(verl. |1⟩) |",
              "|---|---|---|---|---|---|---|---|---|"]
        for k, g in rec.items():
            L.append(f"| {k} | {g['n']} | {_pm(g, 'var_x')} | {_pm(g, 'W00')} | {_f(g.get('W00_bc'))} | "
                     f"{_f(g['W00_moments'])} ± {_f(g['W00_moments_err'])} | {_pm(g, 'eta_fit')} | "
                     f"{_f(g.get('mean_n'))} | {_f(g.get('fidelity_lossy_fock1'))} |")
    cp = r.get("comparison")
    if cp:
        L += ["", "## Vergleich A/B", "",
              f"* Var_A − Var_B = {cp['diff_var_x']['value']:.4f} ± {cp['diff_var_x']['err']:.4f} "
              f"(= η aus dem Vergleich)",
              f"* W_A(0,0) − W_B(0,0) = {cp['diff_W00']['value']:.4f} ± {cp['diff_W00']['err']:.4f}",
              f"* KS-Test: D = {cp['ks']['statistic']:.4f}, p = {cp['ks']['pvalue']:.2e}; Histogramm χ² = "
              f"{cp['histogram']['chi2']:.1f}/{cp['histogram']['ndof']}"]
    d = r.get("drift_AB")
    if d:
        L += ["", "## Drift A/B", ""]
        if "A_halves" in d:
            h = d["A_halves"]
            L.append(f"* A erste ↔ zweite Hälfte: Var {h['var_first']:.4f} → {h['var_second']:.4f} (z = {h['z']:+.2f})")
        if "ABA_chi2" in d:
            L.append(f"* A-B-A: χ² = {d['ABA_chi2']['chi2']:.1f}/{d['ABA_chi2']['ndof']}, max |z| = "
                     f"{d['ABA_chi2']['max_abs_z']:.2f}")
    eb = r.get("efficiency_budget")
    if eb:
        L += ["", "## Effizienzbudget", "",
              f"* Herald-Rate {eb['herald_rate_per_s']:.0f}/s, Dunkelrate {eb['dark_count_rate_per_s']:.0f}/s → "
              f"ξ = {_f(eb['xi'])}", f"* η_el = {_f(eb['eta_el'])}, Modenüberlapp (PCA, rauschkorrigiert) = {_f(eb['eta_mode_pca'])}"]
        if "eta_eff_n" in eb:
            L.append(f"* η_eff aus (Var − ½)/⟨n⟩_ideal = {eb['eta_eff_n']['value']:.4f} ± {_f(eb['eta_eff_n']['err'])} "
                     f"(η_rest beruht hierauf; der |1⟩-Fit gilt für diesen Zustand nicht)")
        if "eta_eff_fit" in eb:
            L.append(f"* η_eff (Fit) = {eb['eta_eff_fit']['value']:.4f} ± {_f(eb['eta_eff_fit']['err'])}; "
                     f"(Var − ½) = {eb['eta_eff_var']['value']:.4f}; η_rest = {eb['eta_rest']['value']:.4f} "
                     f"± {_f(eb['eta_rest']['err'])}")
    ph = r.get("phase") or {}
    if ph.get("available"):
        m, bt, c = ph["metrics"], ph.get("boot", {}), ph["calibration"]
        e = lambda k: (bt.get(k) or {}).get("err")
        q = c.get("quality", {})
        L += ["", "## Phase bekannt (Piezo-Rampe, kalibriert)", "",
              f"* K-θ-Kalibrierung: {len(c.get('ref_blocks', []))} Referenzläufe, Hub {_f(q.get('stroke_rad'), '{:.2f}')} rad, "
              f"Nichtlinearität {_f(q.get('nonlinearity_rad'), '{:.3f}')} rad, Hysterese {_f(q.get('hysteresis_rad'), '{:.3f}')} rad, "
              f"χ²/ndof {_f(q.get('chi2_ndof'), '{:.2f}')}; σ_θ(A) Median {_f(ph['theta_err_A']['median'], '{:.3f}')} rad",
              f"* MaxLik (RρR): {ph['n_records']} Records, {ph['bins']['n_theta']}×{ph['bins']['n_x']} Bins, η_POVM = "
              f"{ph['eta_tomo']:.3f}, N = {ph['N']}, {ph['n_iter']} Iterationen, Bootstrap {len(bt) and 'ja' or 'nein'}",
              f"* W(0,0) = {m['W00']:.4f} ± {_f(e('W00'))}, Parität {m['parity']:.4f} ± {_f(e('parity'))}, "
              f"⟨n⟩ = {m['mean_n']:.3f} ± {_f(e('mean_n'))}, Reinheit {m['purity']:.3f}",
              f"* Quetschung (ρ) {m['squeezing_dB']:+.2f} ± {_f(e('squeezing_dB'), '{:.2f}')} dB, Achse "
              f"{m['axis_rad']:.3f} rad; α aus ⟨a²⟩ = {m['alpha_from_a2']:.3f} ± {_f(e('alpha_from_a2'), '{:.3f}')}"]
        vf = ph["var_theta"]["fit"]
        if vf.get("available") and vf.get("squeezing_dB") is not None:
            L.append(f"* Var(x_θ) der Daten: min {vf['var_min']:.4f}, max {vf['var_max']:.4f} → "
                     f"{vf['squeezing_dB']:+.2f} ± {_f(vf.get('squeezing_dB_err'), '{:.2f}')} dB (Detektor), Achse {vf['axis_rad']:.3f} rad")
        f = ph.get("fidelity") or {}
        if f:
            L.append(f"* Treue zum Sollzustand: ideal {f['F_ideal']:.4f} ± {_f(e('F_ideal'))}, Verlustmodell "
                     f"{f['F_model']:.4f} ± {_f(e('F_model'))} (ξ = {f['model']['xi']:.3f}, η = {f['model']['eta_optical']:.3f})")
        cv = ph["coverage"]
        L.append(f"* Phasenabdeckung: min/Mittel {cv['min_rel']:.2f} ({'ok' if cv['ok'] else 'Lücken'})")
    elif ph:
        L += ["", "## Phase", "", f"* phasengemittelt: {ph.get('reason', 'Phase unbekannt')}"]
    L += ["", "## Warnungen", ""] + ([f"* {w}" for w in r["warnings"]] or ["* keine"])
    t = r["performance"]["timing_s"]
    L += ["", "## Laufzeit", "", "* " + ", ".join(f"{k} {v:.1f} s" for k, v in t.items())]
    return "\n".join(L) + "\n"


def write_report(result: dict, path) -> None:
    Path(path).write_text(render_report(result), encoding="utf-8")


def report_from_file(result_json) -> str:
    return render_report(json.loads(Path(result_json).read_text(encoding="utf-8")))
