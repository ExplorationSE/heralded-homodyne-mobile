"""Wiedergabe-Quelle aus einem Ordner (Stufe 1, ab 0.15): HHD-Sitzungsordner prüfen, Ordner auf dem Mess-PC
durchsuchen und per Drag-and-Drop hochgeladene Sitzungsdateien annehmen.

Geprüft wird nur das HHD-Sitzungsformat (``session.json`` + ``harp_<lauf>.ptu`` je Lauf + ``digitizer.h5``);
Rohdaten der Hersteller-Software werden nicht umgewandelt (Stufe 2). ``truth.h5`` wird nie gelesen oder übernommen.
"""
from __future__ import annotations

import json
import os
import re
import string
from pathlib import Path

LASER_LABEL = {"cw": "CW", "pulsed": "Puls"}
KIND_SHORT = {"vacuum": "K-V", "electronic": "Dunkel", "herald_blocked": "K-H", "heralded": "A", "free_running": "B",
              "random": "B", "state": "Zustand", "phase_reference": "K-θ"}
# Dateien, die beim Hochladen (Drag-and-Drop) angenommen werden; alles andere wird übersprungen
UPLOAD_RE = re.compile(r"^(session\.json|server_session\.json|digitizer\.h5|[A-Za-z0-9_.\-]+\.ptu)$")
FOLDER_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,80}$")


class ReplaySourceError(ValueError):
    pass


def _size_mb(d: Path) -> float:
    n = 0
    for f in d.iterdir():
        try:
            if f.is_file() and f.name != "truth.h5":
                n += f.stat().st_size
        except OSError:
            pass
    return round(n / 1e6, 1)


def inspect_session_dir(path: str | Path) -> dict:
    """Ordner als Wiedergabe-Quelle prüfen. Rückgabe ``dict(ok, msg, path, errors, warnings, info)``.

    Fehler (``ok`` = False) sperren die Übernahme: Ordner fehlt, ``session.json`` fehlt/kein HHD-Format,
    ``digitizer.h5`` fehlt/unlesbar, keine Läufe, PTU-Datei oder Block eines Laufs fehlt."""
    raw_p = str(path or "").strip().strip('"')
    out = dict(ok=False, msg="", path=raw_p, errors=[], warnings=[], info={})
    err = out["errors"]
    if not raw_p:
        err.append("Pfad fehlt")
    else:
        d = Path(os.path.expanduser(raw_p))
        if not d.is_absolute():
            err.append("bitte den vollständigen Pfad angeben (z. B. C:\\HarpADQ32HHDMobil\\hhd_data\\HHD-…)")
        elif not d.exists():
            err.append(f"Ordner nicht gefunden: {d}")
        elif not d.is_dir():
            err.append(f"kein Ordner: {d}")
        else:
            out["path"] = str(d.resolve())
            _inspect(d, out)
    out["ok"] = not err
    if err:
        out["msg"] = "Keine gültige HHD-Sitzung: " + "; ".join(err)
    else:
        i = out["info"]
        out["msg"] = (f"Sitzung erkannt: {i['session_id']} · {i['n_runs']} Läufe ({i['kinds']}) · "
                      f"{i['duration_s']:.0f} s · Laserbetrieb {LASER_LABEL.get(i['laser_mode'], i['laser_mode'])}"
                      + (f" · Laufplan {i['plan_label']}" if i.get("plan_label") else "")
                      + f" · {'simuliert' if i['simulated'] else 'gemessen'} · {i['size_mb']:g} MB")
    return out


def _inspect(d: Path, out: dict) -> None:
    err, warn, info = out["errors"], out["warnings"], out["info"]
    mp = d / "session.json"
    if not mp.exists():
        err.append("session.json fehlt – kein HHD-Sitzungsordner (Rohdaten der Hersteller-Software werden nicht "
                   "umgewandelt)")
        if any(d.glob("*.ptu")) or any(d.glob("*.h5")):
            warn.append("Ordner enthält PTU/HDF5-Dateien, aber kein HHD-Manifest")
        return
    try:
        man = json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        err.append(f"session.json unlesbar: {e}")
        return
    if not isinstance(man, dict) or man.get("format") != "hhd-session":
        err.append("session.json ist kein HHD-Manifest (format != hhd-session)")
        return
    runs = man.get("runs") or []
    if not runs:
        err.append("session.json enthält keine Läufe")
    if not (d / "digitizer.h5").exists():
        err.append("digitizer.h5 fehlt (ADQ32-Daten)")
    missing = [r.get("ptu_file", f"harp_{r.get('id')}.ptu") for r in runs
               if not (d / r.get("ptu_file", f"harp_{r.get('id')}.ptu")).exists()]
    if missing:
        err.append(f"PTU-Datei fehlt ({len(missing)}): {', '.join(missing[:4])}{' …' if len(missing) > 4 else ''}")
    if err:
        return
    from ..io.session import ImportError_, load_session
    try:
        s = load_session(d, verify=False, lazy=True)
    except (ImportError_, OSError, KeyError, ValueError, RuntimeError) as e:
        err.append(f"Sitzung nicht lesbar: {e}")
        return
    for w in s.warnings:
        (err if ("fehlt" in w or "PTU" in w) else warn).append(w)
    if len(s.runs) != len(runs):
        err.append(f"nur {len(s.runs)} von {len(runs)} Läufen in digitizer.h5")
    srv = {}
    if (d / "server_session.json").exists():
        try:
            srv = json.loads((d / "server_session.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            warn.append("server_session.json unlesbar (Laufplan unbekannt)")
    kinds: dict[str, int] = {}
    for r in s.runs:
        k = KIND_SHORT.get(r.kind, r.kind)
        kinds[k] = kinds.get(k, 0) + 1
    if (d / "truth.h5").exists():
        warn.append("truth.h5 vorhanden – wird nie übernommen (Trennung Wahrheit/Auswertung)")
    laser = str(s.device.get("laser_mode") or srv.get("laser_mode") or "cw")
    info.update(session_id=str(s.root_attrs.get("session_id") or man.get("session_id") or d.name), folder=d.name,
                n_runs=len(s.runs), kinds=", ".join(f"{v}× {k}" for k, v in kinds.items()),
                duration_s=float(sum(r.duration_s or 0.0 for r in s.runs)),
                n_records=int(sum(r.n_records_file for r in s.runs)),
                simulated=bool(man.get("simulated", True)), acquisition_mode=man.get("acquisition_mode", ""),
                laser_mode=laser, plan=srv.get("plan") or "", plan_label=srv.get("plan_label") or "",
                note=srv.get("note") or "", size_mb=_size_mb(d), analyzed=(d / "analysis" / "result.json").exists())


def _roots(data_dir: Path) -> list[dict]:
    out = [dict(name="Datenordner (hhd_data)", path=str(data_dir.resolve()))]
    if os.name == "nt":
        for L in string.ascii_uppercase:
            p = f"{L}:\\"
            if os.path.exists(p):
                out.append(dict(name=p, path=p))
    else:
        out.append(dict(name="/", path="/"))
    home = Path.home()
    if home.exists():
        out.append(dict(name="Benutzerordner", path=str(home)))
    return out


def browse_dir(path: str | None, data_dir: Path, limit: int = 400) -> dict:
    """Unterordner von ``path`` (leer: Datenordner) für die Ordnerauswahl in der App; je Eintrag, ob es eine
    HHD-Sitzung ist (``session.json`` vorhanden). Nur Namen, keine Dateiinhalte."""
    p = Path(os.path.expanduser(str(path or "").strip().strip('"'))) if path else data_dir
    if not p.is_absolute():
        raise ReplaySourceError("bitte einen vollständigen Pfad angeben")
    if not p.is_dir():
        raise ReplaySourceError(f"Ordner nicht gefunden: {p}")
    p = p.resolve()
    dirs = []
    try:
        entries = sorted(os.scandir(p), key=lambda e: e.name.lower())
    except OSError as e:
        raise ReplaySourceError(f"Ordner nicht lesbar: {e}") from None
    for e in entries:
        try:
            if not e.is_dir() or e.name.startswith((".", "$")):
                continue
            dirs.append(dict(name=e.name, path=str(Path(e.path)),
                             session=os.path.exists(os.path.join(e.path, "session.json"))))
        except OSError:
            continue
        if len(dirs) >= limit:
            break
    sess = sorted((x for x in dirs if x["session"]), key=lambda x: x["name"].lower(), reverse=True)  # neueste oben
    dirs = sess + [x for x in dirs if not x["session"]]
    parent = str(p.parent) if p.parent != p else ""
    return dict(path=str(p), parent=parent, is_session=(p / "session.json").exists(), dirs=dirs,
                truncated=len(dirs) >= limit, roots=_roots(data_dir))


def upload_target(import_dir: Path, folder: str, name: str) -> Path:
    """Zielpfad einer hochgeladenen Datei unter ``<data_dir>/_import/<folder>/<name>``; nur Sitzungsdateien,
    nie ``truth.h5``, keine Pfadbestandteile."""
    if not FOLDER_RE.match(folder or "") or folder in (".", ".."):
        raise ReplaySourceError("ungültiger Ordnername (erlaubt: Buchstaben, Ziffern, _ . -)")
    if not UPLOAD_RE.match(name or "") or name == "truth.h5" or ".." in name:
        raise ReplaySourceError(f"Datei {name!r} wird nicht übernommen (nur session.json, server_session.json, "
                                "digitizer.h5, *.ptu)")
    return import_dir / folder / name
