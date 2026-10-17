"""Synchronisation und Zuordnung Harp ↔ ADQ32 (Python/NumPy-Referenz, AP9-Schnittstelle).

Schichten:

* ``kernels`` – reine Array-Funktionen ohne Zustand (Gitterindex, Kreuzkorrelation, robuste/stückweise Fits,
  Fenstersuche). Das ist die Schnittstelle des späteren Rust-Kerns: gleiche Namen, gleiche Ein-/Ausgaben
  (float64/int64-Arrays, Ergebnis-Dicts).
* ``markers``, ``clockmap``, ``assign``, ``delay`` – Algorithmen auf Laufebene.
* ``pipeline`` – Sitzung/Kampagne, Bericht, Ausgabe ``sync/sync.h5`` + ``sync/sync.json``.
* ``stream`` – chunkweiser Synchronisierer (Datenstrom, begrenzter Speicher).

Die Wahrheitsdatei wird in diesem Paket nie gelesen (Test ``test_sync_no_truth``).
"""
from __future__ import annotations

import os

BACKENDS = ("python",)


def backend() -> str:
    """Gewählter Kern (``HHD_SYNC_BACKEND``); derzeit nur ``python``; ``rust`` folgt mit AP9."""
    b = os.environ.get("HHD_SYNC_BACKEND", "python").lower()
    if b not in BACKENDS:
        raise RuntimeError(f"HHD_SYNC_BACKEND={b!r} nicht verfügbar (vorhanden: {', '.join(BACKENDS)})")
    return b


def sync_session(*a, **k):
    from .pipeline import sync_session as f
    return f(*a, **k)


def sync_any(*a, **k):
    from .pipeline import sync_any as f
    return f(*a, **k)
