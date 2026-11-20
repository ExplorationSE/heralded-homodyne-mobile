"""Gibt öffentliche Adresse, Zugangscode und (falls 'qrcode' installiert) einen QR-Code aus.

Aufruf: python tools/print_online_info.py <URL> <CODE>
"""
import re
import sys
from pathlib import Path

# Version aus der einzigen Quelle hhd/__init__.py (ohne Paket-Import)
_m = re.search(r'__version__\s*=\s*"([^"]+)"', (Path(__file__).resolve().parent.parent / "hhd" / "__init__.py").read_text("utf-8"))
__version__ = _m.group(1) if _m else "?"


def main() -> None:
    url, code = sys.argv[1], sys.argv[2]
    lokal = len(sys.argv) > 3 and sys.argv[3] == "lokal"     # start_messung ohne -Online
    pretty = code if len(code) != 10 else f"{code[:5]}-{code[5:]}"
    link = f"{url}/?token={code}"
    line = "=" * 76
    print("\n" + line)
    if lokal:
        print(f"  Heralded Homodyne Mobile {__version__} läuft LOKAL (nur an diesem PC erreichbar)")
    else:
        print(f"  Heralded Homodyne Mobile {__version__} ist ONLINE (Cloudflare Quick Tunnel)")
    print(line)
    print(f"  Adresse{' (am Handy öffnen)' if not lokal else ' (Browser hier)   '}:  {url}")
    print(f"  Zugangscode:                {pretty}")
    print(f"  Direkt-Link mit Code:       {link}")
    print(line)
    try:
        import qrcode
        qr = qrcode.QRCode(border=2)
        qr.add_data(link)
        qr.make(fit=True)
        print("  QR-Code (Direkt-Link inkl. Code) mit der Handy-Kamera scannen:\n")
        qr.print_ascii(invert=True)
    except Exception as e:  # QR ist nur Komfort
        print(f"  (QR-Code nicht verfügbar: {e})")
    print("  Den Code bzw. QR-Code nicht weitergeben. Beenden: Enter in diesem Fenster")
    print("  oder stop_online.bat.")
    print(line + "\n")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
