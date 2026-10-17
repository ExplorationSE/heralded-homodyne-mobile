"""Screenshots der Web-App mit headless Chromium (Playwright) gegen einen laufenden HHD-Server.

    python tools/app_screenshots.py http://127.0.0.1:8766 /workspace/homodyne_herald/app_screens [--plan standard]

Ablauf: Steuerung übernehmen → Sitzung starten → Live-Vorschau (Messung) → Ende + Auswertung abwarten →
Ergebnisse, Geräte (inkl. Bestätigungsdialog für einen kritischen Parameter), Protokoll; mobil (390×844) und Desktop.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright


def status(base: str) -> dict:
    with urllib.request.urlopen(base + "/api/status", timeout=10) as r:
        return json.loads(r.read())


def shot(page, path, full=True):
    page.evaluate("document.body.classList.add('shot')")
    page.wait_for_timeout(150)
    page.screenshot(path=path, full_page=full)
    page.evaluate("document.body.classList.remove('shot')")


def tab(page, v):
    page.locator(f".tabs button[data-v='{v}']").dispatch_event("click")


def launch(p):
    try:
        return p.chromium.launch(headless=True)
    except Exception:  # noqa: BLE001 – Fallback: installiertes Chrome
        return p.chromium.launch(headless=True, executable_path="/usr/bin/google-chrome")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base"); ap.add_argument("out")
    ap.add_argument("--plan", default="standard")
    ap.add_argument("--speed", type=float, default=1.0)
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    shots = []
    with sync_playwright() as p:
        br = launch(p)
        mob = br.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True,
                             locale="de-DE",
                             extra_http_headers={"X-Forwarded-For": "203.0.113.7"})   # wie über Tunnel: entfernt
        pg = mob.new_page()
        pg.set_default_timeout(120000)
        pg.goto(a.base + "/", wait_until="networkidle")
        pg.wait_for_function("document.getElementById('conn').classList.contains('ok')")
        pg.wait_for_timeout(500)
        shot(pg, str(out / "01_mobil_start.png"), False); print("…", flush=True) or shots.append("01_mobil_start.png")
        pg.click("#lockbtn"); pg.wait_for_timeout(400)
        pg.select_option("#plan", a.plan); pg.wait_for_timeout(300)
        pg.fill("#speed", str(a.speed))
        pg.click("#start")
        # Live-Vorschau mitten in der Sitzung (nach K-V/Dunkel/K-H in einem A- oder B-Lauf)
        t0 = time.time()
        while time.time() - t0 < 300:
            s = status(a.base)
            runs = s["live"]["runs"]
            if s["state"] == "messung" and len(runs) >= 6:
                break
            if s["state"] in ("fertig", "fehler", "abgebrochen"):
                break
            time.sleep(0.3)
        pg.wait_for_timeout(1200)
        shot(pg, str(out / "02_mobil_messung_live.png")); print("…", flush=True) or shots.append("02_mobil_messung_live.png")
        desk = br.new_context(viewport={"width": 1366, "height": 900}, locale="de-DE")
        dp = desk.new_page(); dp.set_default_timeout(120000); dp.goto(a.base + "/", wait_until="networkidle"); dp.wait_for_timeout(1500)
        shot(dp, str(out / "03_desktop_messung_live.png")); print("…", flush=True) or shots.append("03_desktop_messung_live.png")
        # Ende und Auswertung abwarten
        t0 = time.time()
        while time.time() - t0 < 900:
            s = status(a.base)
            if s["state"] in ("fertig", "abgebrochen", "fehler") and s["jobs"] and all(j["state"] in ("fertig", "fehler") for j in s["jobs"]):
                break
            time.sleep(1)
        print("Status:", s["state"], s["msg"], [(j["session"], j["state"], j["error"]) for j in s["jobs"]])
        pg.wait_for_timeout(800)
        shot(pg, str(out / "04_mobil_messung_fertig.png")); print("…", flush=True) or shots.append("04_mobil_messung_fertig.png")
        tab(pg, "v-erg"); pg.wait_for_timeout(2500)
        shot(pg, str(out / "05_mobil_ergebnisse.png")); print("…", flush=True) or shots.append("05_mobil_ergebnisse.png")
        tab(dp, "v-erg"); dp.wait_for_timeout(2500)
        dp.locator("#togrep").dispatch_event("click"); dp.wait_for_timeout(300)
        shot(dp, str(out / "06_desktop_ergebnisse.png")); print("…", flush=True) or shots.append("06_desktop_ergebnisse.png")
        tab(pg, "v-dev"); pg.wait_for_timeout(800)
        shot(pg, str(out / "07_mobil_geraete.png")); print("…", flush=True) or shots.append("07_mobil_geraete.png")
        # kritischen Parameter aus der Ferne setzen → Bestätigungsdialog (ED-8b)
        loc = pg.evaluate("local")
        sel = "select[data-dev='digitizer'][data-p='input_range_mV']"
        pg.select_option(sel, "1000.0" if pg.locator(sel + " option[value='1000.0']").count() else "1000")
        pg.locator(sel).scroll_into_view_if_needed()
        pg.locator("button[data-set='digitizer'][data-p='input_range_mV']").dispatch_event("click"); pg.wait_for_timeout(800)
        shot(pg, str(out / "08_mobil_geraete_aenderung.png"), False); print("…", flush=True) or shots.append("08_mobil_geraete_aenderung.png")
        if pg.is_visible("#confirm"):
            pg.locator("#cyes").dispatch_event("click"); pg.wait_for_timeout(800)
            shot(pg, str(out / "08b_mobil_geraete_bestaetigt.png"), False); shots.append("08b_mobil_geraete_bestaetigt.png")
        # Triggerausgang aus der Ferne ist gesperrt (Knopf deaktiviert); Server lehnt auch direkte Befehle ab
        tab(pg, "v-log"); pg.wait_for_timeout(1200)
        shot(pg, str(out / "09_mobil_protokoll.png")); print("…", flush=True) or shots.append("09_mobil_protokoll.png")
        br.close()
    print(json.dumps(dict(out=str(out), shots=shots, local_view=loc), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
