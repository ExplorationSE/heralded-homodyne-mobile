"""Screenshots 80_*: Ratenanzeige (Grenzwert, Warnung, Anhalten) – Desktop 1366 px und mobil 390 px.
Server mit Testkonfiguration (Simulator, sim_harp_extra_mcps > harp_max_mcps, langsames Tempo):

    python tools/app_screens_rate.py http://127.0.0.1:8793 <ordner>
"""
import sys, time
from playwright.sync_api import sync_playwright
base, out = sys.argv[1], sys.argv[2]
with sync_playwright() as p:
    br = p.chromium.launch(headless=True, executable_path="/usr/bin/google-chrome")
    pages = {}
    for kind, ctx in (("desktop", dict(viewport={"width": 1366, "height": 900}, locale="de-DE")),
                      ("mobil", dict(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True,
                                     has_touch=True, locale="de-DE"))):
        c = br.new_context(**ctx); pg = c.new_page(); pg.set_default_timeout(60000)
        pg.on("pageerror", lambda e: print("pageerror", e)); pg.on("dialog", lambda d: d.accept())
        pg.goto(base + "/", wait_until="networkidle"); pg.wait_for_timeout(1500)
        pages[kind] = pg

    def shot(tag, need=""):
        for kind, pg in pages.items():
            pg.bring_to_front(); pg.wait_for_timeout(300)        # Hintergrundseiten zeichnen nicht neu
            t1 = time.time()
            while need and time.time() - t1 < 60 and need not in pg.locator("#ratebar").inner_text():
                pg.wait_for_timeout(100)
            pg.evaluate("document.body.classList.add('shot')")
            card = pg.locator("#v-mess .card").first
            card.scroll_into_view_if_needed(); pg.wait_for_timeout(200)
            card.screenshot(path=f"{out}/80_{kind}_rate_{tag}.png")
            print(tag, kind, pg.locator("#ratebar").inner_text()[:160].replace("\n", " | "))

    shot("grenzwert")
    d = pages["desktop"]
    d.locator("#lockbtn").click(); d.wait_for_timeout(500)
    d.select_option("#plan", "kurz"); d.wait_for_timeout(300)
    d.locator("#start").click()
    t0 = time.time()
    while time.time() - t0 < 60 and "ÜBER GRENZWERT" not in d.locator("#ratebar").inner_text():
        d.wait_for_timeout(300)
    shot("warnung", "ÜBER GRENZWERT")
    while time.time() - t0 < 240 and "Angehalten" not in d.locator("#ratebar").inner_text():
        d.wait_for_timeout(500)
    d.wait_for_timeout(1500)
    shot("angehalten")
    br.close()
