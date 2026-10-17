"""Gerätetreiber für den Live-Betrieb (Gesamtplan v1.7, AP Treiber).

* ``hh500`` – PicoQuant **HydraHarp 500** über die native Programmierbibliothek ``HH500Lib``
  (Linux ``libhh500lib.so``, Windows ``HH500Lib.dll``), T2-Modus, FIFO-Auslese.
* ``adq``   – Teledyne SP Devices **ADQ32** über **ADQAPI** (Linux ``libadq.so``, Windows ``ADQAPI.dll``),
  Data-Readout-Schnittstelle (``WaitForRecordBuffer``/``ReturnRecordBuffer``), Parameter über die JSON-Schnittstelle.

Beide Bibliotheken werden **zur Laufzeit** mit ``ctypes`` geladen (keine Abhängigkeit beim Installieren). Fehlt die
Bibliothek, melden die Treiber das mit :class:`LibraryNotFound` und einem klaren Text; der Server lehnt dann den
Live-Betrieb des Geräts ab, ohne abzustürzen. Die Bindungsschicht ist bewusst schmal (eine Klasse je Bibliothek,
nur dokumentierte Funktionen), damit sie später 1:1 durch den Rust-Kern (ED-4) ersetzt werden kann.

Quellen (nur öffentliche Unterlagen, keine Anfrage beim Hersteller):

* HydraHarp 500 – HH500Lib Programming Library for Linux, v1.0.0.0 (PicoQuant, 2025), Kap. 5–7
  (``downloads.picoquant.com/manuals/HydraHarp500_DLL_Linux_Manual.pdf``); HydraHarp 500 Manual, Kap. 6.3.2 (T2).
* ADQ3 Series Digitizers – User Guide 21-2539, Rev. 2026.2, Kap. 10.5, 15 und Anhang A (Strukturen, Funktionen,
  Fehlercodes) (``spdevices.com``).

``mock/`` enthält Nachbildungen beider Bibliotheken in C (gleiche Funktionsnamen, Signaturen, Fehlercodes,
FIFO-/Record-Puffer-Verhalten) für Tests ohne Hardware; sie werden mit ``hhd.drivers.mock.build`` übersetzt.
"""
from ._loader import LibraryNotFound, find_library, load_library

__all__ = ["LibraryNotFound", "find_library", "load_library"]
