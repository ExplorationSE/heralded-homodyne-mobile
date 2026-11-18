"""Übersetzt die Mock-Bibliotheken (``libhh500mock.so``, ``libadqmock.so``; unter Windows ``.dll``) mit gcc/cc.

Unter Windows wird MinGW-w64-gcc verwendet und statisch gelinkt (``-static``), damit die DLL keine
MinGW-Laufzeit-DLLs braucht: Python lädt Abhängigkeiten von DLLs seit 3.8 nicht mehr über ``PATH``.

    python -m hhd.drivers.mock.build [zielverzeichnis]

Die Bibliotheken ersetzen im Test ``libhh500lib.so`` bzw. ``libadq.so`` (Pfad über ``HHD_HH500LIB``/``HHD_ADQAPI``
oder die Server-Konfiguration ``hh500_lib``/``adq_lib``). Nur für Tests/Vorführung – kein Herstellercode.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCES = {"hh500": "hh500_mock.c", "adq": "adq_mock.c"}
WINDOWS = sys.platform.startswith("win")
_EXT = ".dll" if WINDOWS else ".so"
OUTPUT = {"hh500": f"libhh500mock{_EXT}", "adq": f"libadqmock{_EXT}"}


def compiler() -> str | None:
    for c in ("gcc", "cc", "clang"):
        if shutil.which(c):
            return c
    return None


def build(outdir: str | Path, which: tuple[str, ...] = ("hh500", "adq")) -> dict[str, Path]:
    cc = compiler()
    if cc is None:
        raise RuntimeError("kein C-Compiler (gcc/cc/clang) gefunden")
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    out = {}
    for w in which:
        src, dst = HERE / SOURCES[w], outdir / OUTPUT[w]
        if not dst.exists() or dst.stat().st_mtime < max(src.stat().st_mtime, (HERE / "common.h").stat().st_mtime):
            flags = (["-O2", "-shared", "-Wall", "-static", "-static-libgcc", "-Wl,--export-all-symbols"] if WINDOWS
                     else ["-O2", "-shared", "-fPIC", "-Wall"])
            r = subprocess.run([cc, *flags, "-o", str(dst), str(src), "-lm"], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"Übersetzen von {src.name} fehlgeschlagen ({cc}):\n{r.stdout}{r.stderr}")
        out[w] = dst
    return out


if __name__ == "__main__":
    for k, v in build(sys.argv[1] if len(sys.argv) > 1 else HERE / "_build").items():
        print(k, v)
