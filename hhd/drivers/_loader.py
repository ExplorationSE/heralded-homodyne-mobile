"""Suchen und Laden der Herstellerbibliotheken (ctypes), mit klaren Fehlermeldungen."""
from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path


class LibraryNotFound(RuntimeError):
    """Herstellerbibliothek nicht gefunden oder nicht ladbar (Live-Betrieb des Geräts nicht möglich)."""


def _candidates(explicit: str | os.PathLike | None, env: str, names: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    if explicit:
        out.append(str(explicit))
    if os.environ.get(env):
        out.append(os.environ[env])
    plat = "win" if sys.platform.startswith("win") else "linux"
    out += names.get(plat, [])
    return out


def find_library(explicit, env: str, names: dict[str, list[str]]) -> str | None:
    """Erster vorhandener Kandidat (Pfad) oder ``None``; reine Namen ohne Pfad werden dem System-Lader überlassen."""
    for c in _candidates(explicit, env, names):
        p = Path(c)
        if p.is_absolute() or os.sep in c or "/" in c:
            if p.exists():
                return str(p)
        else:
            return c
    return None


def load_library(explicit, env: str, names: dict[str, list[str]], what: str, hint: str) -> ctypes.CDLL:
    """Bibliothek laden; Fehlschlag → :class:`LibraryNotFound` mit allen versuchten Orten und Installationshinweis."""
    tried, errs = [], []
    for c in _candidates(explicit, env, names):
        tried.append(c)
        p = Path(c)
        if (p.is_absolute() or "/" in c or os.sep in c) and not p.exists():
            errs.append(f"{c}: nicht vorhanden")
            continue
        try:
            return ctypes.CDLL(c)          # x86-64: eine Aufrufkonvention (auch unter Windows)
        except OSError as e:
            errs.append(f"{c}: {e}")
    raise LibraryNotFound(f"{what}: Herstellerbibliothek nicht gefunden – versucht: " + "; ".join(errs or tried)
                          + f". {hint} (Pfad auch über die Umgebungsvariable {env} oder die Server-Konfiguration).")
