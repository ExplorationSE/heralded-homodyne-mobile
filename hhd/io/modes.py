"""Betriebsart je Gerät (``sim`` | ``replay`` | ``live``) in den Metadaten jeder Ausgabedatei.

* PTU-Kopf: ``UsrHHD_DeviceMode`` (Betriebsart der HydraHarp für diese Datei), ``UsrHHD_DeviceModes`` (JSON beider
  Geräte), ``UsrHHD_AcquisitionMode`` (``sim`` | ``replay`` | ``live`` | ``mixed``);
* ``digitizer.h5`` (Wurzel): ``device_mode_harp``, ``device_mode_digitizer``, ``acquisition_mode``;
* ``session.json``: ``device_modes`` und ``acquisition_mode``; ``simulated`` ist nur wahr, wenn kein Gerät live ist
  und keine echte Messung wiedergegeben wird (Wiedergabe übernimmt die Angabe der Quelle).
"""
from __future__ import annotations

import json

MODES = ("sim", "replay", "live")
DEVICES = ("harp", "digitizer")
LABEL = {"sim": "SIMULIERT", "replay": "WIEDERGABE", "live": "LIVE", "mixed": "GEMISCHT"}


def check(modes: dict) -> dict:
    out = {}
    for d in DEVICES:
        m = modes.get(d)
        if m not in MODES:
            raise ValueError(f"Betriebsart {d}: {m!r} nicht in {MODES}")
        out[d] = m
    return out


def acquisition_mode(modes: dict) -> str:
    s = {modes[d] for d in DEVICES}
    return s.pop() if len(s) == 1 else "mixed"


def ptu_tags(modes: dict) -> dict:
    from . import ptu as P
    return {"UsrHHD_DeviceMode": (P.TY_ANSI, modes["harp"]),
            "UsrHHD_DeviceModes": (P.TY_ANSI, json.dumps(modes, sort_keys=True)),
            "UsrHHD_AcquisitionMode": (P.TY_ANSI, acquisition_mode(modes))}


def h5_attrs(modes: dict) -> dict:
    return dict(device_mode_harp=modes["harp"], device_mode_digitizer=modes["digitizer"],
                acquisition_mode=acquisition_mode(modes))


SIM = {"harp": "sim", "digitizer": "sim"}
