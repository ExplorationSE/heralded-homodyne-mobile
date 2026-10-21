"""Alle 23 Laufpläne mit Quellmodell (0.15) einmal komplett über einen eigenen Test-Server durchlaufen.

Je Plan Lauf A (mit Herald) und Lauf B (ohne Herald): W(0,0), Var(x), Mandel Q phasengemittelt gegen Soll, bei Plänen
mit Phase zusätzlich Quetschung aus dem Var(x_θ)-Fit und aus ρ für A und B. Robust gegen Unterbrechung wie
``tools/phase_runs_015.py``: erledigte Pläne (``ergebnisse.json``) werden übersprungen, nach jedem Plan werden
``ergebnisse.json`` und ``ergebnisse.md`` neu geschrieben, Sitzungsdaten gelöscht (``--keep plan:engine`` behält sie).

    nohup python tools/quelle_runs_015.py --out /workspace/homodyne_herald/quelle_0.15 \\
        --work /workspace/hhd_quelle_runs > quelle.log 2>&1 &

Nur Simulation; der Server läuft mit Zugangscode auf 127.0.0.1 und wird am Ende beendet.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from phase_runs_015 import log, start_server  # noqa: E402

PLANS_ORDER = ["kurzphase_squeezed_6dB", "kurzphase_squeezed_3dB", "kurzphase_cat_even_2", "kurzphase_cat_odd_2",
               "kurzphase_cat_even_1", "kurzphase_cat_odd_1", "kurzphase_kitten_3dB", "kurzphase_coherent_1",
               "kurzphase_fock01", "kurz", "kurz_fock2", "kurz_fock3", "kurz_fock01", "kurz_cat_even_1",
               "kurz_cat_odd_1", "kurz_cat_even_2", "kurz_cat_odd_2", "kurz_kitten_3dB", "kurz_coherent_1",
               "kurz_squeezed_3dB", "kurz_squeezed_6dB", "kurz_thermal_0.5", "standard"]
RUNS = [(p, "batch") for p in PLANS_ORDER] + [("kurzphase_squeezed_6dB", "stream"), ("kurzphase_cat_even_2", "stream")]


async def run_plan(port: int, token: str, plan: str, eng: str, timeout_s: float) -> str:
    import websockets
    url = f"ws://127.0.0.1:{port}/ws?token={token}"
    async with websockets.connect(url, origin=f"http://127.0.0.1:{port}", max_size=None) as ws:
        async def cmd(c, p=None):
            await ws.send(json.dumps(dict(type="command", cmd=c, params=p or {}, name="quelle_runs_015", id=c)))
            while True:
                m = json.loads(await ws.recv())
                if m.get("type") == "ack" and m.get("id") == c:
                    if not m.get("ok"):
                        raise RuntimeError(f"{c}: {m.get('msg')}")
                    return m
        await cmd("acquire_control")
        await cmd("set_display", dict(sim_engine=eng, auto_analyze=True))
        r = await cmd("start_session", dict(plan=plan, speed=50, note=f"0.15 Quellmodell {plan} ({eng})"))
        sid = r.get("session_id"); t0 = time.time(); last = None
        log("gestartet", plan, eng, sid)
        while time.time() - t0 < timeout_s:
            m = json.loads(await asyncio.wait_for(ws.recv(), 300))
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


def _q(o):
    """Validierungseintrag auf value/err/true/pull reduzieren."""
    if not isinstance(o, dict) or "value" not in o:
        return None
    return {k: o.get(k) for k in ("value", "err", "true", "pull")}


def make_row(d: Path) -> dict:
    from hhd.sim.analysis_validate import validate_analysis
    meta = json.loads((d / "server_session.json").read_text(encoding="utf-8"))
    res = json.loads((d / "analysis" / "result.json").read_text(encoding="utf-8"))
    v = validate_analysis(d, res)
    st = res.get("state") or {}
    src = st.get("source") or {}
    row = dict(sid=d.name, plan=meta["plan"], eng=meta.get("sim_engine"), preset=meta["state"]["preset"],
               label=meta["state"]["label"].replace("|", "\\|"), source=src.get("model"), source_dB=src.get("dB"),
               t=res.get("performance", {}).get("timing_s", {}).get("total"))
    for g in ("A", "B"):
        G, R = v["groups"].get(g, {}), res.get("reconstruction", {}).get(g, {})
        row[g] = dict(n=G.get("n"), **{q: _q(G.get(q)) for q in ("W00", "var_x", "mandel_Q")},
                      Q=R.get("mandel_Q"), Q_err=R.get("mandel_Q_err"))
    P, vp = res.get("phase") or {}, v.get("phase") or {}
    if P.get("available"):
        row["phase"] = dict(A=dict(var_fit_dB=_q(vp.get("var_fit_dB")), rho_dB=_q(vp.get("squeezing_dB")),
                                   rho_W00=_q(vp.get("W00")), rho_Q=_q(vp.get("mandel_Q")), F_exp=vp.get("fidelity_expected"), N=P.get("N"),
                                   n_iter=P.get("n_iter"), n=P.get("n_records")))
        pb, vb = P.get("B") or {}, vp.get("B") or {}
        if pb.get("available"):
            row["phase"]["B"] = dict(var_fit_dB=_q(vb.get("var_fit_dB")), rho_dB=_q(vb.get("squeezing_dB")),
                                     rho_W00=_q(vb.get("W00")), rho_Q=_q(vb.get("mandel_Q")), N=pb.get("N"), n_iter=pb.get("n_iter"), n=pb.get("n_records"))
        else:
            row["phase"]["B"] = dict(na=pb.get("reason"))
    elif P:
        row["phase"] = dict(na=P.get("reason"))
    return row


def write_table(rows: list, out: Path) -> None:
    json.dump(rows, open(out / "ergebnisse.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    rows = sorted(rows, key=lambda r: (RUNS.index((r["plan"], r["eng"])) if (r["plan"], r["eng"]) in RUNS else 99))

    def vt(o, k=3):
        if not o:
            return "–"
        e = "–" if o.get("err") is None else f"{o['err']:.{k}f}"
        return (f"{o['value']:+.{k}f} ± {e} / {o['true']:+.{k}f}"
                + (f" (z {o['pull']:+.1f})" if o.get("pull") is not None else ""))

    def sig(G):
        q, e = G.get("Q"), G.get("Q_err")
        if q is None or not e:
            return "–"
        return f"{q / e:+.1f}"
    L = ["# Ergebnisse 0.15 – Quellmodell für Lauf B, Mandel Q, Quetschung", "",
         "Eigener Test-Server (Simulation; batch = Datei im Speicher, stream = Datenstrom), je Plan ein kompletter "
         "Durchlauf mit automatischer Auswertung (Server-Standard). Werte „gemessen ± Fehler / Soll (z)“, "
         "z = (gemessen − Soll)/Fehler. Q/σ = Mandel Q geteilt durch seinen Bootstrap-Fehler (App-Bewertung: "
         "Q < 0 mit ≥ 3σ = nichtklassisch, sub-poissonsch). Quellmodell: spdc = Paarquelle (B ≈ thermisch/Vakuum), "
         "squeezed = OPO mit Photonsubtraktion (B = gequetschtes Vakuum der Quelle), test = Zustand dauerhaft "
         "erzeugt, Herald nur Zeitmarke (B = A). Erzeugt mit `tools/quelle_runs_015.py`.", "",
         "## Phasengemittelt (alle Pläne)", "",
         "| Plan | Sim | Quelle | A W(0,0) | A Var(x) | A Mandel Q | A Q/σ | B W(0,0) | B Var(x) | B Mandel Q | B Q/σ |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for x in rows:
        A, B = x["A"], x["B"]
        src = f"{x['source']}" + (f" {x['source_dB']:.2f} dB" if x.get("source_dB") else "")
        L.append(f"| {x['plan']} | {x['eng']} | {src} | {vt(A['W00'], 4)} | {vt(A['var_x'])} | {vt(A['mandel_Q'])} | "
                 f"{sig(A)} | {vt(B['W00'], 4)} | {vt(B['var_x'])} | {vt(B['mandel_Q'])} | {sig(B)} |")
    L += ["", "## Phasenaufgelöst (Pläne „Kurz mit Phase“): Quetschung in dB", "",
          "Var-Fit = 10·log10(Var_min/½) aus dem Var(x_θ)-Fit am Detektor (Soll mit η·η_el); ρ = aus MaxLik-ρ "
          "(η_el korrigiert, Soll mit η des Laufs). > 0 dB = keine Quetschung. Mandel Q ρ = aus der Diagonale von ρ (η_el "
          "korrigiert); bei Piezo-Rampe ist das phasengemittelte Q oben für phasenempfindliche Zustände durch die "
          "ungleiche θ-Abdeckung verzerrt, maßgeblich ist dann Q aus ρ.", "",
          "| Plan | Sim | A Var-Fit [dB] | A ρ [dB] | A W(0,0) ρ | A Mandel Q ρ | B Var-Fit [dB] | B ρ [dB] | B W(0,0) ρ | B Mandel Q ρ |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for x in rows:
        P = x.get("phase")
        if not P:
            continue
        if "na" in P:
            L.append(f"| {x['plan']} | {x['eng']} | Phase nicht verfügbar: {P['na']} ||||||||"); continue
        A, B = P["A"], P.get("B") or {}
        bcols = (f"nicht verfügbar: {B.get('na')} | – | – | –" if "na" in B else
                 f"{vt(B.get('var_fit_dB'), 2)} | {vt(B.get('rho_dB'), 2)} | {vt(B.get('rho_W00'), 4)} | {vt(B.get('rho_Q'))}")
        L.append(f"| {x['plan']} | {x['eng']} | {vt(A.get('var_fit_dB'), 2)} | {vt(A.get('rho_dB'), 2)} | "
                 f"{vt(A.get('rho_W00'), 4)} | {vt(A.get('rho_Q'))} | {bcols} |")
    L += ["", "Technische Angaben je Lauf (Sitzung, N, MaxLik-Iterationen, Records, Auswertezeit):", ""]
    for x in rows:
        P = x.get("phase") or {}
        s = f"* {x['plan']} ({x['eng']}, {x['sid']}): A {x['A'].get('n')} / B {x['B'].get('n')} Records"
        if "A" in P:
            s += f"; Phase A N={P['A'].get('N')}, Iter {P['A'].get('n_iter')}, {P['A'].get('n')} Records"
        if "B" in P and "na" not in P["B"]:
            s += f"; Phase B N={P['B'].get('N')}, Iter {P['B'].get('n_iter')}, {P['B'].get('n')} Records"
        if x.get("t") is not None:
            s += f"; Auswertung {x['t']:.0f} s"
        L.append(s)
    tmp = out / "ergebnisse.md.tmp"
    tmp.write_text("\n".join(L) + "\n", encoding="utf-8"); tmp.replace(out / "ergebnisse.md")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8798)
    ap.add_argument("--token", default="qRuns015")
    ap.add_argument("--out", default="/workspace/homodyne_herald/quelle_0.15")
    ap.add_argument("--work", default="/workspace/hhd_quelle_runs")
    ap.add_argument("--keep", default="", help="plan:engine,… nicht löschen, sondern nach <work>/keep verschieben")
    ap.add_argument("--only", default="", help="nur diese Pläne (Komma-Liste)")
    ap.add_argument("--timeout", type=float, default=5400)
    a = ap.parse_args()
    out, work = Path(a.out), Path(a.work)
    out.mkdir(parents=True, exist_ok=True); (work / "keep").mkdir(parents=True, exist_ok=True)
    keep = {tuple(x.split(":")) for x in a.keep.split(",") if x}
    jp = out / "ergebnisse.json"
    rows = json.loads(jp.read_text(encoding="utf-8")) if jp.exists() else []
    done = {(r["plan"], r.get("eng")) for r in rows}
    todo = [r for r in RUNS if r not in done and (not a.only or r[0] in a.only.split(","))]
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
            log("fertig", plan, eng, f"{time.time() - t:.0f} s")
    finally:
        srv.terminate()
        try:
            srv.wait(30)
        except Exception:
            srv.kill()
    log("FERTIG")
    return 0


if __name__ == "__main__":
    sys.exit(main())
