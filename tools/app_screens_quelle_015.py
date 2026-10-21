"""Screenshots 60_*: Quellmodell, Bewertung Quanteneffekte (Desktop 1366 px, mobil 390 px), gegen einen laufenden Server.

    python tools/app_screens_quelle_015.py http://127.0.0.1:8799 <token> <ordner> '{"gequetscht_6dB": "<sitzung>", ...}'
"""
import json, sys, time, urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright
base, token, out = sys.argv[1], sys.argv[2], Path(sys.argv[3])
sess = json.loads(sys.argv[4])          # {name: session_id}
out.mkdir(parents=True, exist_ok=True)
shots = []

def shot(pg, name, full=True, el=None):
    pg.evaluate("document.body.classList.add('shot')"); pg.wait_for_timeout(200)
    p = str(out / name)
    if el:
        loc = pg.locator(el); loc.scroll_into_view_if_needed(timeout=5000); loc.screenshot(path=p, timeout=15000)
    else:
        pg.screenshot(path=p, full_page=full)
    pg.evaluate("document.body.classList.remove('shot')"); shots.append(p); print(p, flush=True)

def launch(p):
    try:
        return p.chromium.launch(headless=True)
    except Exception:
        return p.chromium.launch(headless=True, executable_path="/usr/bin/google-chrome")

with sync_playwright() as p:
    br = launch(p)
    for kind, ctx in (("desktop", dict(viewport={"width": 1366, "height": 900}, locale="de-DE")),
                      ("mobil", dict(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True,
                                     has_touch=True, locale="de-DE"))):
        c = br.new_context(**ctx); pg = c.new_page(); pg.set_default_timeout(60000)
        pg.goto(f"{base}/?token={token}", wait_until="networkidle")
        pg.wait_for_function("document.getElementById('conn').classList.contains('ok')"); pg.wait_for_timeout(800)
        # Laufplan mit Quellmodell in der Beschreibung
        pg.click("#lockbtn"); pg.wait_for_timeout(600)
        pg.select_option("#plan", "kurzphase_cat_even_2"); pg.dispatch_event("#plan", "change"); pg.wait_for_timeout(600)
        shot(pg, f"60_{kind}_laufplan_quellmodell.png", full=False)
        pg.locator(".tabs button[data-v='v-erg']").dispatch_event("click"); pg.wait_for_timeout(1500)
        for name, sid in sess.items():
            pg.evaluate(f"openSession({json.dumps(sid)})"); pg.wait_for_timeout(3500)
            shot(pg, f"60_{kind}_ergebnis_{name}.png")
            shot(pg, f"60_{kind}_bewertung_{name}.png", el="#resverdict")
        shot(pg, f"60_{kind}_sitzungsliste.png", full=False) if kind == "desktop" else None
        c.close()
    br.close()
print(json.dumps(shots, ensure_ascii=False))
