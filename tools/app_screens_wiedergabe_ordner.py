"""Screenshots 92_*: Wiedergabe aus Ordner (Kasten Sitzung → „Aus Ordner wiedergeben“), gegen einen laufenden
Simulations-Server, Desktop 1366 px und mobil 390 px.

    python tools/app_screens_wiedergabe_ordner.py http://127.0.0.1:8792 <token> <ausgabeordner> <sitzungsordner> <fehlerordner>
"""
import json, sys
from pathlib import Path
from playwright.sync_api import sync_playwright
base, token, out, good, bad = sys.argv[1], sys.argv[2], Path(sys.argv[3]), sys.argv[4], sys.argv[5]
out.mkdir(parents=True, exist_ok=True)
shots = []

def shot(pg, name, el=None):
    pg.evaluate("document.body.classList.add('shot')"); pg.wait_for_timeout(250)
    p = str(out / name)
    if el:
        loc = pg.locator(el); loc.scroll_into_view_if_needed(timeout=5000); loc.screenshot(path=p, timeout=15000)
    else:
        pg.screenshot(path=p, full_page=False)
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
        if pg.locator("#lockbtn").inner_text().startswith("Steuerung übernehmen"):
            pg.click("#lockbtn"); pg.wait_for_timeout(600)
        pg.evaluate("document.getElementById('rsrc').open = true"); pg.wait_for_timeout(300)
        shot(pg, f"92_{kind}_ordner_leer.png", el="#v-mess .card >> nth=0")
        pg.click("#rsrc_browse"); pg.wait_for_timeout(900)
        shot(pg, f"92_{kind}_ordner_durchsuchen.png", el="#v-mess .card >> nth=0")
        pg.click("#rsrc_list [data-x]"); pg.fill("#rsrc_path", bad); pg.click("#rsrc_check"); pg.wait_for_timeout(900)
        shot(pg, f"92_{kind}_ordner_fehler.png", el="#v-mess .card >> nth=0")
        pg.fill("#rsrc_path", good); pg.click("#rsrc_check"); pg.wait_for_timeout(1200)
        shot(pg, f"92_{kind}_ordner_erkannt.png", el="#v-mess .card >> nth=0")
        pg.click("#rsrc_take"); pg.wait_for_timeout(2500)
        shot(pg, f"92_{kind}_wiedergabe_uebernommen.png")
        pg.click("#start"); pg.wait_for_timeout(4000)
        shot(pg, f"92_{kind}_wiedergabe_laeuft.png")
        pg.wait_for_function("!['vorbereitung','messung','schreiben','pause'].includes(S.state)", timeout=300000)
        pg.wait_for_timeout(500)
        pg.click("#rsrc_clear"); pg.wait_for_timeout(2500)
        shot(pg, f"92_{kind}_zurueck_konfiguration.png", el="#v-mess .card >> nth=0") if kind == "desktop" else None
        c.close()
    br.close()
print(json.dumps(shots, ensure_ascii=False))
