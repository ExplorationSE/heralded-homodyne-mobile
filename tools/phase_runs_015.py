"""Alle Laufpläne „Kurz mit Phase“ (kurzphase_*) einmal komplett über einen eigenen Test-Server durchlaufen (0.15).

Robust gegen Unterbrechung: bereits erledigte Pläne (in ``ergebnisse.json``) werden übersprungen, nach jedem Plan
werden ``ergebnisse.json`` und ``ergebnisse.md`` sofort neu geschrieben und die Sitzungsdaten des Plans gelöscht
(Ausnahme ``--keep``: Sitzung wird nach ``<work>/keep/`` verschoben, z. B. für Screenshots).

    python tools/phase_runs_015.py --port 8797 --out /workspace/homodyne_herald/phase_0.15 \\
        --work /tmp/hhd_phase_runs --keep kurzphase_cat_even_2:batch,kurzphase_squeezed_6dB:batch

Nur Simulation; der Server läuft mit Zugangscode auf 127.0.0.1 und wird am Ende beendet.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ORDER = ["cat_even_2", "squeezed_6dB", "cat_even_1", "cat_odd_1", "cat_odd_2", "kitten_3dB", "coherent_1",
         "squeezed_3dB", "fock01"]
RUNS = [(f"kurzphase_{k}", "batch") for k in ORDER] + [("kurzphase_squeezed_6dB", "stream"),
                                                       ("kurzphase_cat_even_2", "stream")]


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- Server
def server_toml(work: Path, port: int) -> Path:
    src = (ROOT / "config" / "server.toml").read_text(encoding="utf-8")
    lines = []
    for ln in src.splitlines():
        k = ln.split("=")[0].strip()
        if k == "sim_config":
            ln = f'sim_config = "{(ROOT / "config" / "standard.yaml").as_posix()}"'
        elif k == "sim_record_truth":
            ln = "sim_record_truth = true"
        lines.append(ln)
    p = work / "server.toml"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def start_server(work: Path, port: int, token: str) -> subprocess.Popen:
    cfg = server_toml(work, port)
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    f = open(work / "server.log", "a", encoding="utf-8")
    p = subprocess.Popen([sys.executable, "-m", "hhd.server", "--config", str(cfg), "--host", "127.0.0.1",
                          "--port", str(port), "--token", token, "--data-dir", str(work / "data"),
                          "--log-dir", str(work / "logs")], cwd=str(work), env=env, stdout=f, stderr=subprocess.STDOUT)
    import urllib.request
    for _ in range(120):
        time.sleep(1)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/login", timeout=2)
            return p
        except Exception:
            if p.poll() is not None:
                raise RuntimeError("Server beendet, siehe server.log")
    raise RuntimeError("Server antwortet nicht")


async def run_plan(port: int, token: str, plan: str, eng: str, timeout_s: float) -> str:
    import websockets
    url = f"ws://127.0.0.1:{port}/ws?token={token}"
    async with websockets.connect(url, origin=f"http://127.0.0.1:{port}", max_size=None) as ws:
        async def cmd(c, p=None):
            await ws.send(json.dumps(dict(type="command", cmd=c, params=p or {}, name="phase_runs_015", id=c)))
            while True:
                m = json.loads(await ws.recv())
                if m.get("type") == "ack" and m.get("id") == c:
                    if not m.get("ok"):
                        raise RuntimeError(f"{c}: {m.get('msg')}")
                    return m
        await cmd("acquire_control")
        await cmd("set_display", dict(sim_engine=eng, auto_analyze=True))
        r = await cmd("start_session", dict(plan=plan, speed=50, note=f"0.15 Phase {plan} ({eng})"))
        sid = r.get("session_id"); t0 = time.time(); last = None
        log("gestartet", plan, eng, sid)
        while time.time() - t0 < timeout_s:
            m = json.loads(await asyncio.wait_for(ws.recv(), 120))
            if m.get("type") != "status":
                continue
            jobs = [j for j in m.get("jobs", []) if j.get("session") == sid]
            key = (m.get("state"), tuple(j.get("state") for j in jobs))
            if key != last:
                log("  ", *key); last = key
            if jobs and all(j.get("state") in ("fertig", "fehler") for j in jobs) and m.get("state") in ("fertig", "abgebrochen", "fehler"):
                if any(j.get("state") == "fehler" for j in jobs):
                    raise RuntimeError(f"Auswertung fehlgeschlagen: {jobs}")
                return sid
        raise TimeoutError(f"{plan}: nicht fertig nach {timeout_s:.0f} s")


# ---------------------------------------------------------------- Tabelle
def make_row(d: Path) -> dict:
    from hhd.sim.analysis_validate import validate_analysis
    meta = json.loads((d / "server_session.json").read_text(encoding="utf-8"))
    res = json.loads((d / "analysis" / "result.json").read_text(encoding="utf-8"))
    P = res.get("phase", {})
    base = dict(sid=d.name, plan=meta["plan"], eng=meta.get("sim_engine"), preset=meta["state"]["preset"],
                label=meta["state"]["label"].replace("|", "\\|"))
    if not P.get("available"):
        return dict(base, na=P.get("reason"))
    vp = validate_analysis(d, res).get("phase", {})
    return dict(base, n=P["n_records"], N=P["N"], F_exp=vp.get("fidelity_expected"), F_ideal=P["fidelity"].get("F_ideal"),
                F_model=P["fidelity"].get("F_model"), F_err=(P["boot"].get("F_model") or {}).get("err"),
                **{q: vp.get(q) for q in ("W00", "squeezing_dB", "alpha_from_a2", "mean_n", "axis_rad")},
                th=vp.get("theta"), vfit=P["var_theta"]["fit"], cov=P["coverage"], cal=P["calibration"]["quality"],
                warn=P["warnings"], t=res["performance"]["timing_s"])


def write_table(rows: list, out: Path) -> None:
    json.dump(rows, open(out / "ergebnisse.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    f = lambda x, k=4: "–" if x is None else f"{x:.{k}f}"

    def vt(o, k=3):
        if not o:
            return "–"
        return (f"{o['value']:+.{k}f} ± {f(o.get('err'), k)} / {o['true']:+.{k}f}"
                + (f" (z {o['pull']:+.1f})" if o.get("pull") is not None else ""))
    L = ["# Ergebnisse 0.15 – Laufpläne „Kurz mit Phase“ (Piezo-Rampe, K-θ, MaxLik)", "",
         "Eigener Test-Server (Simulation, Datei im Speicher = batch bzw. Datenstrom = stream), je Plan ein kompletter "
         "Durchlauf (K-V, Dunkel, K-θ, K-H, A 10 s, K-θ, B, A 10 s, K-θ, K-V) mit automatischer Auswertung "
         "(`AnalysisParams(n_boot=100)`, Bootstrap Tomografie 20). Soll = ρ_soll = ξ·D_σ(L_η(ρ_t)) + (1−ξ)|0⟩⟨0| "
         "(η des Laufs, ξ Herald-Reinheit, σ = gemessener θ-Fehler); Werte „gemessen ± Fehler / Soll (z)“. "
         "α = √|⟨a²⟩| nur für Katzen (sonst verzerrt, Soll 0). Quetschung: 10·log10(min_θ Var/½) aus ρ, > 0 = keine "
         "Quetschung. θ-Fehler: kalibriertes θ gegen wahres θ je Record (alle Läufe mit Rampe); σ_ber. = berichtete "
         "Unsicherheit (Median). Erzeugt mit `tools/phase_runs_015.py`.", "",
         "| Zustand | Sim | A-Records | F(ρ, ρ_soll) | F Verlustmodell (Auswertung) | F ideal | W(0,0) | Quetschung ρ [dB] | α aus ⟨a²⟩ | ⟨n⟩ | θ-Fehler rms / σ_ber. [rad] |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for x in rows:
        if "na" in x:
            L.append(f"| {x['label']} | {x['eng']} | Phase nicht verfügbar: {x['na']} |||||||||"); continue
        th = x["th"] or {}
        L.append(f"| {x['label']} | {x['eng']} | {x['n']} | {f(x['F_exp'])} | {f(x['F_model'])} ± {f(x['F_err'])} | "
                 f"{f(x['F_ideal'], 3)} | {vt(x['W00'], 4)} | {vt(x['squeezing_dB'], 2)} | "
                 f"{vt(x['alpha_from_a2'], 3) if x['preset'].startswith('cat') else '–'} | {vt(x['mean_n'], 3)} | "
                 f"{f(th.get('rms'), 3)} / {f(th.get('sigma_reported'), 3)} |")
    L += ["", "Weitere Kennwerte je Plan:", ""]
    for x in rows:
        if "na" in x:
            continue
        c, vf, cv, ax = x["cal"], x["vfit"], x["cov"], x.get("axis_rad")
        L.append(f"* **{x['label']}** ({x['eng']}, {x['sid']}): K-θ Hub {c['stroke_rad']:.2f} rad, Nichtlinearität "
                 f"{c['nonlinearity_rad']:.3f}, Hysterese {c['hysteresis_rad']:.3f} rad, χ²/ndof {c['chi2_ndof']:.2f}, "
                 f"θ-Bias {f((x['th'] or {}).get('bias'), 3)} rad; Var(x_θ)-Fit am Detektor "
                 f"{f(vf.get('squeezing_dB'), 2)} ± {f(vf.get('squeezing_dB_err'), 2)} dB"
                 + (f", Achse {ax['value']:.3f} (Soll {ax['true']:.3f}) rad" if ax else "")
                 + f"; Abdeckung min/Mittel {cv['min_rel']:.2f} ({'ok' if cv['ok'] else 'Lücken'}); N = {x['N']}; "
                 f"Auswertung gesamt {x['t'].get('total', 0):.0f} s, davon Phase {x['t'].get('phase', 0):.1f} s"
                 + (f"; Warnungen: {'; '.join(x['warn'])}" if x["warn"] else ""))
    tmp = out / "ergebnisse.md.tmp"
    tmp.write_text("\n".join(L) + "\n", encoding="utf-8"); tmp.replace(out / "ergebnisse.md")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8797)
    ap.add_argument("--token", default="phRuns015")
    ap.add_argument("--out", default="/workspace/homodyne_herald/phase_0.15")
    ap.add_argument("--work", default="/tmp/hhd_phase_runs")
    ap.add_argument("--keep", default="", help="plan:engine,… nicht löschen, sondern nach <work>/keep verschieben")
    ap.add_argument("--timeout", type=float, default=3600)
    a = ap.parse_args()
    out, work = Path(a.out), Path(a.work)
    out.mkdir(parents=True, exist_ok=True); (work / "keep").mkdir(parents=True, exist_ok=True)
    keep = {tuple(x.split(":")) for x in a.keep.split(",") if x}
    jp = out / "ergebnisse.json"
    rows = json.loads(jp.read_text(encoding="utf-8")) if jp.exists() else []
    done = {(r["plan"], r.get("eng")) for r in rows}
    todo = [r for r in RUNS if r not in done]
    log("erledigt:", sorted(done)); log("offen:", todo)
    if not todo:
        write_table(rows, out); log("FERTIG (nichts offen)"); return 0
    srv = start_server(work, a.port, a.token)
    log("Server", a.port, "PID", srv.pid)
    try:
        for plan, eng in todo:
            t = time.time()
            try:
                sid = asyncio.run(run_plan(a.port, a.token, plan, eng, a.timeout))
                d = work / "data" / sid
                row = make_row(d)
            except Exception as e:                       # Plan fehlgeschlagen: protokollieren, weiter mit dem nächsten
                log("FEHLER", plan, eng, repr(e)); continue
            rows = [r for r in rows if (r["plan"], r.get("eng")) != (plan, eng)] + [row]
            write_table(rows, out)
            if (plan, eng) in keep:
                shutil.move(str(d), str(work / "keep" / sid)); log("  behalten:", work / "keep" / sid)
            else:
                shutil.rmtree(d, ignore_errors=True)
            log("fertig", plan, eng, f"{time.time() - t:.0f} s", "F_soll =", row.get("F_exp"))
    finally:
        srv.terminate()
        try:
            srv.wait(30)
        except subprocess.TimeoutExpired:
            srv.kill()
    log("FERTIG")
    return 0


if __name__ == "__main__":
    sys.exit(main())
