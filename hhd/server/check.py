"""Geräteprüfung: Treiber laden, Geräte öffnen und abfragen – **ohne zu messen** – und sauber schließen.

    python -m hhd.server.check --config config/server_live.toml [--alle] [--json DATEI]
    hhd check-devices --config config/server_live.toml

Je Gerät: Treiberpfad, Treiber-/Bibliotheksversion, Gerät gefunden, Modell, Seriennummer, Firmware/Hardware-Version;
HydraHarp 500: Zahl der Eingänge, Basisauflösung, Sync-Rate; ADQ32: Abtastrate, Kanäle, Produktoptionen.
Fehler werden eingeordnet (Treiber fehlt, Gerät belegt, kein Gerät, sonstiger Fehler) und mit Abhilfe gemeldet.

Geprüft werden die als ``live`` konfigurierten Geräte (``device_mode`` bzw. ``harp_mode``/``digitizer_mode``),
mit ``--alle`` beide. Exitcode 0 nur, wenn alle geprüften Geräte in Ordnung sind (2: Konfigurationsfehler,
3: kein Gerät als live konfiguriert und ``--alle`` nicht gesetzt). Ergebnis zusätzlich als JSON
(Standard ``<log_dir>/geraetepruefung.json``).

Was die Prüfung am Gerät tut:

* HydraHarp 500: ``HH500_OpenDevice``, ``HH500_Initialize`` (T2, interne Referenz – nötig für die Abfragen, startet
  keine Messung), ``GetHardwareInfo``, ``GetNumOfInputChannels``, ``GetBaseResolution``, ``GetSyncRate`` (nach 150 ms),
  ``HH500_CloseDevice``.
* ADQ32: ``ADQAPI_ValidateVersion``, ``CreateADQControlUnit``, ``ListDevices``, ``SetupDevice``, ``GetParametersString``
  (Konstanten, Taktsystem) – keine Parameter werden geschrieben –, ``DeleteADQControlUnit``.

**[unsicher, am Gerät prüfen]**: Fehlercodes/Texte der echten Bibliotheken für „Gerät belegt“ (eingeordnet über
den Fehlertext: BUSY/LOCKED/IN USE bzw. ADQ: gelistet, aber ``SetupDevice`` scheitert) und die JSON-Schlüssel der
ADQ-Konstanten (``product_name``, ``serial_number``, ``firmware``, ``nof_channels``; Taktsystem ``sampling_frequency``).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")

KAT = {"ok": "in Ordnung", "treiber_fehlt": "Treiber/Bibliothek nicht gefunden", "belegt": "Gerät belegt",
       "kein_geraet": "kein Gerät gefunden", "fehler": "Fehler", "nicht_geprueft": "nicht geprüft"}


# ---------------------------------------------------------------- Pfade (Laufwerk D: fehlt)
def drive_missing(p: str) -> bool:
    """True, wenn ``p`` mit einem Windows-Laufwerk beginnt, das es nicht gibt (unter Linux/macOS: immer)."""
    if not p or not DRIVE_RE.match(p):
        return False
    if sys.platform.startswith("win"):
        return not Path(p[:3]).exists()
    return True


def resolve_paths(cfg, root: Path | None = None) -> list[str]:
    """``data_dir``, ``log_dir``, ``adq_trace_dir`` mit fehlendem Laufwerk auf ``.\\hhd_data`` bzw. ``.\\logs``
    umlenken; Rückgabe: Meldungen (für Konsole/Log)."""
    root = Path(root) if root else Path.cwd()
    msgs = []
    alt = {"data_dir": "hhd_data", "log_dir": "logs", "adq_trace_dir": "logs/adq_trace"}
    for k, a in alt.items():
        v = getattr(cfg, k, "")
        if drive_missing(v):
            setattr(cfg, k, a)
            msgs.append(f"Hinweis: Laufwerk {v[:2]} nicht vorhanden – {k} = {v!r} → {(root / a)} "
                        f"(in der Server-Konfiguration anpassen, falls die Daten woanders hin sollen)")
    return msgs


# ---------------------------------------------------------------- Einordnung der Fehler
def _hint_lib(what: str, env: str, key: str, default: str) -> str:
    return (f"{what}-Bibliothek installieren bzw. den Pfad angeben: Umgebungsvariable {env} oder {key} in der "
            f"Server-Konfiguration (z. B. config/server_live.toml) setzen. Üblicher Installationsort: {default}.")


HINT = {
    "harp": dict(
        treiber_fehlt=_hint_lib("HH500Lib (PicoQuant)", "HHD_HH500LIB", "hh500_lib",
                                r"Windows C:\Windows\System32\HH500Lib.dll (Setup der HH500Lib/HydraHarp-500-Software), "
                                "Linux /opt/picoquant/hh500lib/libhh500lib.so"),
        belegt="Das Gerät ist von anderer Software belegt: HydraHarp-500-Software (oder eine andere Instanz des "
               "Mess-Servers/der Geräteprüfung) schließen und erneut prüfen.",
        kein_geraet="Keine HydraHarp 500 gefunden: USB-Kabel (USB 3) und Netzteil prüfen, Gerät einschalten, im "
                    "Windows-Gerätemanager nachsehen (Treiber installiert?); bei mehreren Geräten hh500_devidx bzw. "
                    "hh500_serial prüfen. Ist die HydraHarp-Software geöffnet, diese schließen.",
        fehler="Gerät neu einschalten und Prüfung wiederholen; Debug-Ausgabe im Log-Verzeichnis (HH500_SaveDebugDump) "
               "und docs/HARDWARE.md beachten."),
    "digitizer": dict(
        treiber_fehlt=_hint_lib("ADQAPI (Teledyne SP Devices)", "HHD_ADQAPI", "adq_lib",
                                r"Windows C:\Program Files\SP Devices\ADQAPI_x64\ADQAPI.dll (SDK-Installer), Linux libadq.so"),
        belegt="Der ADQ32 wird gelistet, lässt sich aber nicht einrichten – vermutlich von anderer Software belegt "
               "(Digitizer Studio, ADQAssist, Beispielprogramme, zweite Server-Instanz): diese schließen und erneut prüfen.",
        kein_geraet="Kein ADQ-Digitizer gefunden: Karte im PCIe-Steckplatz bzw. Thunderbolt/PCIe-Kabel prüfen, Rechner "
                    "neu starten, im Windows-Gerätemanager den Teledyne-SP-Devices-Treiber prüfen; bei mehreren Karten "
                    "adq_device_index prüfen.",
        fehler="ADQAPI-Trace-Log im adq_trace_dir lesen; Rechner neu starten; SDK-Version prüfen (docs/HARDWARE.md)."),
}
BUSY_WORDS = ("BUSY", "LOCKED", "IN USE", "IN_USE", "USED BY", "BELEGT")


def _classify_harp(msg: str) -> str:
    u = msg.upper()
    if "DEVICE_BUSY_MEASURING" not in u and any(w in u for w in BUSY_WORDS):
        return "belegt"
    if "OPEN_FAIL" in u or "NICHT GEFUNDEN" in u:
        return "kein_geraet"
    return "fehler"


def _base(kind: str, mode: str) -> dict:
    name = "HydraHarp 500" if kind == "harp" else "ADQ32"
    return dict(kind=kind, name=name, mode=mode, checked=True, ok=False, category="fehler", library_path="",
                library_found=False, library_version="", device_found=False, model="", serial="", firmware="",
                details={}, error="", hint="", duration_s=0.0)


def _finish(r: dict, cat: str, err: str = "") -> dict:
    r["category"], r["ok"] = cat, cat == "ok"
    r["error"] = err
    if cat == "belegt":
        r["device_found"] = True          # vorhanden, aber von anderer Software geöffnet
    r["hint"] = "" if cat == "ok" else HINT[r["kind"]].get(cat, "")
    return r


# ---------------------------------------------------------------- HydraHarp 500
def check_harp(lib_path: str = "", devidx: int = 0, serial: str = "", mode: str = "live") -> dict:
    from ..drivers import LibraryNotFound, find_library
    from ..drivers import hh500 as H
    r = _base("harp", mode); t0 = time.time()
    r["library_path"] = find_library(lib_path or None, H.ENV, H.LIB_NAMES) or ""
    try:
        lib = H.HH500Lib(lib_path or None)
    except LibraryNotFound as e:
        r["duration_s"], r["library_path"] = time.time() - t0, ""
        return _finish(r, "treiber_fehlt", str(e))
    r["library_found"], r["library_path"] = True, str(lib.path)
    try:
        r["library_version"] = lib.library_version()
    except Exception as e:  # noqa: BLE001
        r["library_version"] = f"? ({e})"
    dev = H.HH500Device(lib, devidx, serial or None)
    try:
        hw = dev.open(H.REF_INTERNAL)
        r["device_found"] = True
        r["model"], r["serial"] = hw.get("model", ""), hw.get("serial", "")
        r["firmware"] = hw.get("version", "")
        d = dict(n_inputs=hw.get("n_channels"), sync_input=True, partno=hw.get("partno", ""),
                 base_resolution_ps=hw.get("base_resolution_ps"), devidx=hw.get("devidx"))
        time.sleep(0.15)                        # Zählraten erst ≥ 100 ms nach Initialize gültig (Handbuch)
        try:
            import ctypes as C
            v = C.c_int(0)
            lib.call("HH500_GetSyncRate", dev.devidx, C.byref(v))
            d["sync_rate_per_s"] = int(v.value)
        except Exception as e:  # noqa: BLE001
            d["sync_rate_per_s"] = None; d["sync_rate_error"] = str(e)
        r["details"] = d
        _finish(r, "ok")
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        _finish(r, _classify_harp(msg), msg)
        r["device_found"] = r["category"] == "belegt" or (r["category"] != "kein_geraet" and dev.is_open)
    finally:
        try:
            dev.close()
        except Exception as e:  # noqa: BLE001
            r["details"]["close_error"] = str(e)
    r["duration_s"] = round(time.time() - t0, 3)
    return r


# ---------------------------------------------------------------- ADQ32
def check_adq(lib_path: str = "", device_index: int = 0, trace_dir: str = "", mode: str = "live") -> dict:
    from ..drivers import LibraryNotFound, find_library
    from ..drivers import adq as A
    r = _base("digitizer", mode); t0 = time.time()
    r["library_path"] = find_library(lib_path or None, A.ENV, A.LIB_NAMES) or ""
    try:
        lib = A.ADQAPILib(lib_path or None)
    except LibraryNotFound as e:
        r["duration_s"], r["library_path"] = time.time() - t0, ""
        return _finish(r, "treiber_fehlt", str(e))
    r["library_found"], r["library_path"] = True, str(lib.path)
    rev = lib.revision()
    v = lib.validate_version()
    vc = {0: "kompatibel", -2: "rückwärtskompatibel", -1: "inkompatibel"}.get(v, str(v))
    r["library_version"] = (rev or "?") + f" (API {A.ADQAPI_VERSION_MAJOR}.{A.ADQAPI_VERSION_MINOR}: {vc})"
    if v == -1:
        r["duration_s"] = time.time() - t0
        return _finish(r, "treiber_fehlt", f"ADQAPI-Version inkompatibel zu {A.ADQAPI_VERSION_MAJOR}."
                                           f"{A.ADQAPI_VERSION_MINOR} – passendes SDK installieren")
    import ctypes as C
    dll = lib.dll
    cu = dll.CreateADQControlUnit()
    try:
        if not cu:
            return _finish(r, "fehler", "CreateADQControlUnit fehlgeschlagen (Treiber/Kernelmodul geladen?)")
        if trace_dir:
            os.makedirs(trace_dir, exist_ok=True)
            dll.ADQControlUnit_EnableErrorTrace(cu, A.LOG_LEVEL_INFO, str(trace_dir).encode())
        lst, n = C.c_void_p(), C.c_uint(0)
        if not dll.ADQControlUnit_ListDevices(cu, C.byref(lst), C.byref(n)):
            return _finish(r, "fehler", "ADQControlUnit_ListDevices fehlgeschlagen")
        r["details"]["n_devices_listed"] = int(n.value)
        if n.value <= device_index:
            return _finish(r, "kein_geraet", f"kein ADQ-Digitizer mit Index {device_index} ({n.value} gelistet)")
        r["device_found"] = True
        if not dll.ADQControlUnit_SetupDevice(cu, int(device_index)):
            return _finish(r, "belegt", f"ADQControlUnit_SetupDevice({device_index}) fehlgeschlagen "
                                        f"(Gerät gelistet, aber nicht einrichtbar)")
        dev = A.ADQDevice(lib, device_index)
        dev.cu, dev.adq_num = cu, 1
        const = dev.get_params("CONSTANT")
        try:
            clk = dev.get_params("CLOCK_SYSTEM")
        except Exception as e:  # noqa: BLE001
            clk = {"_error": str(e)}
        fw = const.get("firmware") or {}
        r["model"] = str(const.get("product_name", ""))
        r["serial"] = str(const.get("serial_number", ""))
        r["firmware"] = " ".join(str(x) for x in (fw.get("type", ""), fw.get("name", ""), fw.get("revision", "")) if x) \
            if isinstance(fw, dict) else str(fw)
        fs = clk.get("sampling_frequency")
        r["details"] |= dict(n_channels=const.get("nof_channels"), sampling_rate_Hz=float(fs) if fs else None,
                             product_options=const.get("product_options", ""),
                             reference_source=clk.get("reference_source"), device_index=int(device_index))
        if "_error" in clk:
            r["details"]["clock_error"] = clk["_error"]
        return _finish(r, "ok")
    except Exception as e:  # noqa: BLE001
        return _finish(r, "fehler", f"{type(e).__name__}: {e}")
    finally:
        if cu:
            dll.DeleteADQControlUnit(cu)
        r["duration_s"] = round(time.time() - t0, 3)


# ---------------------------------------------------------------- beide
def device_modes(cfg) -> dict:
    return {"harp": cfg.harp_mode or cfg.device_mode, "digitizer": cfg.digitizer_mode or cfg.device_mode}


def check_devices(cfg, root: Path | None = None, all_devices: bool = False, kinds=None) -> dict:
    """Prüfung nach Server-Konfiguration; Rückgabe der Zusammenfassung (JSON-fähig)."""
    root = Path(root) if root else Path.cwd()
    absd = lambda p: str(p if Path(p).is_absolute() else root / p) if p else ""
    modes = device_modes(cfg)
    want = list(kinds) if kinds else [k for k in ("harp", "digitizer") if all_devices or modes[k] == "live"]
    res = []
    for k in ("harp", "digitizer"):
        if k not in want:
            r = _base(k, modes[k]); r.update(checked=False, ok=True, category="nicht_geprueft",
                                             error="", hint=f"Betriebsart {modes[k]} – nicht als live konfiguriert")
            res.append(r); continue
        if k == "harp":
            res.append(check_harp(cfg.hh500_lib, cfg.hh500_devidx, cfg.hh500_serial, modes[k]))
        else:
            res.append(check_adq(cfg.adq_lib, cfg.adq_device_index, absd(cfg.adq_trace_dir), modes[k]))
    checked = [r for r in res if r["checked"]]
    from .. import __version__
    return dict(schema="hhd-geraetepruefung/1.0", software=__version__, time=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                host=__import__("platform").node(),
                modes=modes, all_devices=bool(all_devices), devices=res, n_checked=len(checked),
                ok=bool(checked) and all(r["ok"] for r in checked))


def format_text(s: dict) -> str:
    L = [f"Geräteprüfung (hhd {s['software']}, {s['time']}) – Betriebsart HydraHarp: {s['modes']['harp']}, "
         f"ADQ32: {s['modes']['digitizer']}", ""]
    for r in s["devices"]:
        head = f"{r['name']}: "
        if not r["checked"]:
            L += [head + f"nicht geprüft ({r['hint']})", ""]; continue
        L.append(head + ("OK" if r["ok"] else f"FEHLER – {KAT.get(r['category'], r['category'])}"))
        L.append(f"  Treiber:        {r['library_path'] if r['library_found'] else 'nicht gefunden'}")
        if r["library_found"]:
            L.append(f"  Version:        {r['library_version'] or '–'}")
        L.append(f"  Gerät gefunden: {('ja, aber belegt' if r['category'] == 'belegt' else 'ja') if r['device_found'] else 'nein'}")
        if r["ok"]:
            L.append(f"  Modell:         {r['model'] or '–'}")
            L.append(f"  Seriennummer:   {r['serial'] or '–'}")
            L.append(f"  Firmware:       {r['firmware'] or '–'}" + ("  (Hardware-Version laut GetHardwareInfo)" if r["kind"] == "harp" else ""))
            d = r["details"]
            if r["kind"] == "harp":
                sr = d.get("sync_rate_per_s")
                L.append(f"  Eingänge:       {d.get('n_inputs')} + Sync (Basisauflösung {d.get('base_resolution_ps')} ps, "
                         f"Sync-Rate {sr if sr is not None else '–'} /s)")
            else:
                fs = d.get("sampling_rate_Hz")
                L.append(f"  Kanäle:         {d.get('n_channels')}  Abtastrate: {fs / 1e9:.2f} GS/s" if fs else
                         f"  Kanäle:         {d.get('n_channels')}  Abtastrate: –")
                if d.get("product_options"):
                    L.append(f"  Optionen:       {d['product_options']}")
        else:
            L.append(f"  Meldung:        {r['error']}")
            L.append(f"  Abhilfe:        {r['hint']}")
        L.append("")
    if not s["n_checked"]:
        L.append("Ergebnis: kein Gerät als live konfiguriert – nichts geprüft (--alle prüft beide).")
    else:
        L.append("Ergebnis: " + ("alle geprüften Geräte in Ordnung." if s["ok"] else
                                 "NICHT in Ordnung – Messung so nicht starten."))
    return "\n".join(L)


def write_json(s: dict, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


def main(argv=None) -> int:
    from .service import load_config
    ap = argparse.ArgumentParser(prog="python -m hhd.server.check",
                                 description="Geräteprüfung ohne Messung (Treiber laden, Geräte öffnen/abfragen/schließen)")
    ap.add_argument("--config", default="config/server_live.toml")
    ap.add_argument("--alle", action="store_true", help="beide Geräte prüfen, auch wenn nicht als live konfiguriert")
    ap.add_argument("--json", help="JSON-Datei (Standard <log_dir>/geraetepruefung.json)")
    ap.add_argument("--hh500-lib"); ap.add_argument("--adq-lib")
    a = ap.parse_args(argv)
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    try:
        if not Path(a.config).exists():
            raise FileNotFoundError(f"Konfiguration {a.config} nicht gefunden")
        cfg = load_config(a.config)
    except Exception as e:  # noqa: BLE001
        print(f"Geräteprüfung: Konfiguration fehlerhaft – {e}", file=sys.stderr)
        return 2
    if a.hh500_lib:
        cfg.hh500_lib = a.hh500_lib
    if a.adq_lib:
        cfg.adq_lib = a.adq_lib
    for m in resolve_paths(cfg):
        print(m)
    s = check_devices(cfg, Path.cwd(), all_devices=a.alle)
    s["config"] = str(a.config)
    print(format_text(s))
    jp = Path(a.json) if a.json else Path(cfg.log_dir) / "geraetepruefung.json"
    try:
        print(f"\nZusammenfassung: {write_json(s, jp)}")
    except OSError as e:
        print(f"\nZusammenfassung nicht geschrieben ({jp}): {e}", file=sys.stderr)
    if not s["n_checked"]:
        return 3
    return 0 if s["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
