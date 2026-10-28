"""Screenshots 90_*: Laserbetrieb Puls/CW im Kasten Sitzung, Wigner-Funktion A und B (Desktop 1366 px, mobil 390 px).
Server im Simulator-Modus (eigene Konfiguration mit leerem data_dir empfohlen):

    python tools/app_screens_laser_wigner.py http://127.0.0.1:8796 <ordner> [--plan kurz] [--puls-plan kurzphase_cat_even_1]
"""
import argparse, json, time, urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser(); ap.add_argument("base"); ap.add_argument("out")
ap.add_argument("--plan", default="kurz"); ap.add_argument("--puls-plan", default="kurzphase_cat_even_1")
ap.add_argument("--speed", default="20")
ap.add_argument("--nur-puls", action="store_true", help="nur Schritt 3 (Puls-Sitzung)")
ap.add_argument("--puls-sid", default="", help="vorhandene Puls-Sitzung verwenden (ohne neue Messung)")
a = ap.parse_args(); base = a.base; out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
shots = []
CTX = {"desktop": dict(viewport={"width": 1366, "height": 900}, locale="de-DE"),
       "mobil": dict(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True, locale="de-DE")}


def status():
    with urllib.request.urlopen(base + "/api/status", timeout=10) as r:
        return json.loads(r.read())


def wait_final(t=900):
    t0 = time.time()
    while time.time() - t0 < t:
        s = status()
        if s["state"] in ("fertig", "abgebrochen", "fehler") and s["jobs"] and all(j["state"] in ("fertig", "fehler") for j in s["jobs"]):
            return s
        time.sleep(1)
    return status()


def shot_union(pg, name, sels):
    """Ausschnitt über die Vereinigung der Elemente (ganze Seite, damit mobil gestapelte Karten passen)."""
    pg.evaluate("document.body.classList.add('shot')"); pg.wait_for_timeout(250)
    boxes = []
    for s in sels:
        el = pg.locator(s).first; el.scroll_into_view_if_needed(); pg.wait_for_timeout(100)
        boxes.append(pg.evaluate("e => { const r = e.getBoundingClientRect(); return [r.left + scrollX, r.top + scrollY, r.right + scrollX, r.bottom + scrollY]; }", el.element_handle()))
    x0 = max(0, min(b[0] for b in boxes) - 6); y0 = max(0, min(b[1] for b in boxes) - 6)
    x1 = max(b[2] for b in boxes) + 6; y1 = max(b[3] for b in boxes) + 6
    p = str(out / name)
    pg.screenshot(path=p, full_page=True, clip=dict(x=x0, y=y0, width=x1 - x0, height=y1 - y0))
    pg.evaluate("document.body.classList.remove('shot')"); shots.append(p); print(p, flush=True)


def open_page(br, kind):
    c = br.new_context(**CTX[kind]); pg = c.new_page(); pg.set_default_timeout(60000)
    pg.on("pageerror", lambda e: print("pageerror", kind, e)); pg.on("dialog", lambda d: d.accept())
    pg.goto(base + "/", wait_until="networkidle")
    pg.wait_for_function("document.getElementById('conn').classList.contains('ok')"); pg.wait_for_timeout(800)
    return pg


def take(pg):
    if "abgeben" not in pg.locator("#lockbtn").inner_text().lower():
        pg.click("#lockbtn"); pg.wait_for_timeout(600)


def release(pg):
    if "abgeben" in pg.locator("#lockbtn").inner_text().lower():
        pg.click("#lockbtn"); pg.wait_for_timeout(600)


def run(pg, plan, laser):
    pg.locator(".tabs button[data-v='v-mess']").dispatch_event("click"); pg.wait_for_timeout(500)
    take(pg)
    pg.select_option("#plan", plan); pg.dispatch_event("#plan", "change"); pg.wait_for_timeout(400)
    pg.select_option("#laser", laser); pg.dispatch_event("#laser", "change"); pg.wait_for_timeout(400)
    pg.fill("#speed", a.speed); pg.wait_for_timeout(200)
    pg.click("#start"); pg.wait_for_timeout(1500)
    s = wait_final()
    print("Status:", s["state"], s["msg"], [(j["session"], j["state"], j["error"]) for j in s["jobs"]], flush=True)
    return s


def results(pg, sid):
    pg.locator(".tabs button[data-v='v-erg']").dispatch_event("click"); pg.wait_for_timeout(1500)
    pg.evaluate(f"openSession({json.dumps(sid)})"); pg.wait_for_timeout(4000)


with sync_playwright() as p:
    try:
        br = p.chromium.launch(headless=True)
    except Exception:
        br = p.chromium.launch(headless=True, executable_path="/usr/bin/google-chrome")
    pages = {k: open_page(br, k) for k in CTX}
    sid_cw = None
    if not a.nur_puls:
        # 1) Kasten Sitzung mit Laserbetrieb Puls und CW
        for kind, pg in pages.items():
            pg.bring_to_front(); take(pg)
            pg.select_option("#plan", a.plan); pg.dispatch_event("#plan", "change"); pg.wait_for_timeout(400)
            for lm, tag in (("pulsed", "laserbetrieb"), ("cw", "laserbetrieb_cw")):
                pg.select_option("#laser", lm); pg.dispatch_event("#laser", "change"); pg.wait_for_timeout(900)
                shot_union(pg, f"90_{kind}_{tag}.png", ["#v-mess .card"])
            release(pg)
        # 2) CW-Sitzung (Kurzplan) → Wigner A und B
        d = pages["desktop"]; d.bring_to_front()
        s = run(d, a.plan, "cw"); sid_cw = max(j["session"] for j in s["jobs"]); release(d)
        for kind, pg in pages.items():
            pg.bring_to_front(); results(pg, sid_cw)
            shot_union(pg, f"90_{kind}_wigner_ab.png", [".card:has(#c-wig)", ".card:has(#c-wigB)"])
    sid_p = a.puls_sid
    if not sid_p:
        # 3) Puls-Sitzung (Phase bekannt) → Quellmodell-/Laserbetrieb-Hinweis und Wigner A/B aus ρ
        d = pages["desktop"]; d.bring_to_front()
        d.locator(".tabs button[data-v='v-mess']").dispatch_event("click"); d.wait_for_timeout(500); take(d)
        d.select_option("#plan", a.puls_plan); d.dispatch_event("#plan", "change")
        d.select_option("#laser", "pulsed"); d.dispatch_event("#laser", "change"); d.wait_for_timeout(900)
        shot_union(d, "90_desktop_laser_puls_sitzung.png", ["#v-mess .card"])
        s = run(d, a.puls_plan, "pulsed"); sid_p = max(j["session"] for j in s["jobs"]); release(d)
    for kind, pg in pages.items():
        pg.bring_to_front(); results(pg, sid_p)
        shot_union(pg, f"90_{kind}_laser_puls_hinweis.png", ["#resstate"])
        shot_union(pg, f"90_{kind}_laser_puls_wigner_ab.png", [".card:has(#c-wig)", ".card:has(#c-wigB)"])
    br.close()
print(json.dumps(dict(shots=shots, cw=sid_cw, puls=sid_p), ensure_ascii=False))
