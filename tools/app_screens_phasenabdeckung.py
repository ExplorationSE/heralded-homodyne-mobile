"""Screenshots 70_*: Karte Phasenabdeckung (Desktop 1366 px Diagrammbereich, mobil 390 px zu/aufgeklappt).

    python tools/app_screens_phasenabdeckung.py http://127.0.0.1:8799 <token> <sitzung> <ordner>
"""
import sys, json
from playwright.sync_api import sync_playwright
base, token, sid, out = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
with sync_playwright() as p:
    br = p.chromium.launch(headless=True, executable_path="/usr/bin/google-chrome")
    for kind, ctx in (("desktop", dict(viewport={"width": 1366, "height": 900}, locale="de-DE")),
                      ("desktop1024", dict(viewport={"width": 1024, "height": 800}, locale="de-DE")),
                      ("mobil", dict(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True, locale="de-DE"))):
        c = br.new_context(**ctx); pg = c.new_page(); pg.set_default_timeout(60000)
        pg.on("pageerror", lambda e: print("pageerror", e))
        pg.goto(f"{base}/?token={token}", wait_until="networkidle"); pg.wait_for_timeout(800)
        pg.locator(".tabs button[data-v='v-erg']").dispatch_event("click"); pg.wait_for_timeout(1000)
        pg.evaluate(f"openSession({json.dumps(sid)})"); pg.wait_for_timeout(3500)
        pg.evaluate("document.body.classList.add('shot')")
        info = pg.evaluate("""() => { const g = document.querySelector('#resbox .grid'), cards = [...g.children].filter(e => !e.classList.contains('hidden'));
            return {last: cards[cards.length-1].id, cov: document.getElementById('ph-cov').getBoundingClientRect().width, grid: g.getBoundingClientRect().width,
                    open: document.getElementById('covdet').open, cols: getComputedStyle(document.querySelector('.covwrap')).gridTemplateColumns,
                    textcols: getComputedStyle(document.querySelector('.covcols')).gridTemplateColumns,
                    plotW: document.getElementById('c-cov').clientWidth, refW: document.getElementById('c-vth').clientWidth,
                    plotH: document.getElementById('c-cov').clientHeight, refH: document.getElementById('c-vth').clientHeight,
                    plotBlockH: document.querySelector('.covplot').getBoundingClientRect().height,
                    textH: document.querySelector('.covtext').getBoundingClientRect().height,
                    refCardH: document.getElementById('ph-var').getBoundingClientRect().height,
                    covCardH: document.getElementById('ph-cov').getBoundingClientRect().height,
                    font: getComputedStyle(document.querySelector('#phtbl td')).fontSize}; }""")
        print(kind, info)
        if kind.startswith("desktop"):
            pg.locator("#resbox .grid").screenshot(path=f"{out}/70_{kind}_phasenabdeckung.png")
        else:
            loc = pg.locator("#ph-cov"); loc.scroll_into_view_if_needed(); loc.screenshot(path=f"{out}/70_mobil_phasenabdeckung.png")
            pg.locator("#covdet summary").click(); pg.wait_for_timeout(300)
            loc.screenshot(path=f"{out}/70_mobil_phasenabdeckung_aufgeklappt.png")
        c.close()
    br.close()
