"""HDF5-Kompression der Rohdaten (ab 0.15): Blosc-LZ4 mit Bitshuffle über ``hdf5plugin``, Rückfall gzip.

Messung (Box, 21.10.2026, Records 5000 × int16 aus der Simulation): gzip-4 schreibt 46 MB/s und liest 157 MB/s,
Blosc-LZ4+Bitshuffle schreibt 1056 MB/s und liest 1485 MB/s bei 0,57 statt 0,65 der Rohgröße. gzip-4 begrenzt
damit K-V-Läufe mit 10 kHz Trigger (104 MB/s) im Datenstrom; Blosc nicht.

* Wahl: Umgebung ``HHD_H5_COMPRESSION`` > :func:`set_compression` (Server: ``h5_compression`` in ``server.toml``) >
  Standard ``blosc-lz4``. Werte: ``blosc-lz4`` | ``gzip`` (gzip-4 wie bis 0.14) | ``none``.
* Fehlt ``hdf5plugin``, wird mit gzip geschrieben (Hinweis über :func:`status`).
* **Lesen:** gzip-Dateien gehen immer (in HDF5 eingebaut); Blosc-Dateien brauchen ``hdf5plugin`` – wird beim Import
  von ``hhd.io`` registriert. Fremdprogramme (HDFView, h5dump) brauchen das Blosc-Filter-Plugin (HDF5-Filter 32001).
"""
from __future__ import annotations

import os
import warnings

CHOICES = ("blosc-lz4", "gzip", "none")
BLOSC_ID = 32001
_choice: str | None = None
_registered: bool | None = None
_warned = False


def register() -> bool:
    """``hdf5plugin`` laden (registriert die Filter im HDF5 dieses Prozesses). True, wenn verfügbar."""
    global _registered
    if _registered is None:
        try:
            import hdf5plugin  # noqa: F401
            _registered = True
        except Exception:                                  # nicht installiert oder defekt → gzip
            _registered = False
    return _registered


def set_compression(name: str | None) -> None:
    global _choice
    if name is not None and name not in CHOICES:
        raise ValueError(f"h5_compression: {' | '.join(CHOICES)} (nicht {name!r})")
    _choice = name


def requested() -> str:
    env = os.environ.get("HHD_H5_COMPRESSION", "").strip().lower()
    if env:
        if env not in CHOICES:
            raise ValueError(f"HHD_H5_COMPRESSION: {' | '.join(CHOICES)} (nicht {env!r})")
        return env
    return _choice or "blosc-lz4"


def effective() -> str:
    """Tatsächlich verwendete Kompression (blosc-lz4 ohne hdf5plugin → gzip)."""
    c = requested()
    if c == "blosc-lz4" and not register():
        global _warned
        if not _warned:
            warnings.warn("hdf5plugin nicht installiert – digitizer.h5 wird mit gzip geschrieben (langsamer); "
                          "pip install hdf5plugin", RuntimeWarning, stacklevel=3)
            _warned = True
        return "gzip"
    return c


def kwargs(n: int = 1) -> dict:
    """``create_dataset``-Argumente für Rohdaten (leer bei n = 0)."""
    if not n:
        return {}
    c = effective()
    if c == "gzip":
        return dict(compression="gzip", compression_opts=4)
    if c == "blosc-lz4":
        import hdf5plugin
        return dict(**hdf5plugin.Blosc(cname="lz4", clevel=5, shuffle=hdf5plugin.Blosc.BITSHUFFLE))
    return {}


def dataset_filters(ds) -> list[int]:
    """Filter-IDs eines Datensatzes (z. B. 1 = gzip, 32001 = Blosc)."""
    pl = ds.id.get_create_plist()
    return [pl.get_filter(i)[0] for i in range(pl.get_nfilters())]


def describe(ds) -> str:
    f = dataset_filters(ds)
    return "blosc-lz4" if BLOSC_ID in f else ("gzip" if 1 in f else ("none" if not f else ",".join(map(str, f))))


def check_readable(ds, path="") -> None:
    """Klare Meldung statt HDF5-Fehler, wenn eine Blosc-Datei ohne hdf5plugin gelesen werden soll."""
    if BLOSC_ID in dataset_filters(ds) and not register():
        raise RuntimeError(f"{path}: Rohdaten sind mit Blosc komprimiert – bitte 'pip install hdf5plugin' "
                           "(HDFView/h5dump brauchen das Blosc-Filter-Plugin)")


def status() -> dict:
    return dict(requested=requested(), effective=effective(), hdf5plugin=bool(register()))
