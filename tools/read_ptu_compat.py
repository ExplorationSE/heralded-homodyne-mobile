"""Read_PTU-kompatibler Leser (eigene Implementierung, kein PicoQuant-Code).

Liest PTU-Dateien Record für Record nach demselben Algorithmus wie der öffentliche PicoQuant-Demo-Leser
``Read_PTU.py`` (Funktion ``readHT2(2)``: Bitstring-Zerlegung special/channel/timetag, Überlaufkorrektur,
Marker 1..15, Sync = Kanal 0, Eingänge = channel + 1) und schreibt dessen Textformat
(``<record#> CHN <ch> <truetime> <ps>``, ``<record#> MAR <bits> <truetime>``, ``<record#> OFL * <n>``).
Damit lässt sich die Ausgabe zeilengenau mit dem Originalskript vergleichen (Test ``test_read_ptu_official``,
wenn ``HHD_READ_PTU_PY`` auf eine Kopie von ``Read_PTU.py`` zeigt).

Aufruf: ``python tools/read_ptu_compat.py datei.ptu ausgabe.txt``
"""
from __future__ import annotations

import struct
import sys

T2WRAPAROUND_V2 = 33554432


def read_records(path: str):
    with open(path, "rb") as f:
        data = f.read()
    assert data[:6] == b"PQTTTR", "keine PTU-Datei"
    pos, n_rec, glob_res = 16, None, None
    while True:
        name = data[pos:pos + 32].rstrip(b"\0").decode(); pos += 32
        idx, typ = struct.unpack_from("<iI", data, pos); pos += 8
        if typ == 0x4001FFFF or (typ & 0xFFFF) == 0xFFFF:
            n = struct.unpack_from("<q", data, pos)[0]; pos += 8 + n
        elif typ == 0x20000008:
            v = struct.unpack_from("<d", data, pos)[0]; pos += 8
            if name == "MeasDesc_GlobalResolution":
                glob_res = v
        else:
            v = struct.unpack_from("<q", data, pos)[0]; pos += 8
            if name == "TTResult_NumberOfRecords":
                n_rec = v
            if name == "TTResultFormat_TTTRRecType":
                assert v == 0x00010207, "nur Generic T2"
        if name == "Header_End":
            break
    events = []      # (recNum, typ, a, b)
    ofl = 0
    for rec_num in range(n_rec):
        bits = "{0:0{1}b}".format(struct.unpack_from("<I", data, pos + 4 * rec_num)[0], 32)
        special, channel, timetag = int(bits[0:1], 2), int(bits[1:7], 2), int(bits[7:32], 2)
        if special == 1:
            if channel == 0x3F:
                n = 1 if timetag == 0 else timetag
                ofl += T2WRAPAROUND_V2 * n
                events.append((rec_num, "OFL", n, None))
            if 1 <= channel <= 15:
                events.append((rec_num, "MAR", channel, ofl + timetag))
            if channel == 0:
                events.append((rec_num, "CHN", 0, ofl + timetag))
        else:
            events.append((rec_num, "CHN", channel + 1, ofl + timetag))
    return events, glob_res


def write_text(events, glob_res, out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as o:
        for rec_num, typ, a, b in events:
            if typ == "OFL":
                o.write("%u OFL * %2x\n" % (rec_num, a))
            elif typ == "MAR":
                o.write("%u MAR %2x %u\n" % (rec_num, a, b))
            else:
                o.write("%u CHN %1x %u %8.0lf\n" % (rec_num, a, b, b * glob_res * 1e12))


if __name__ == "__main__":
    ev, gr = read_records(sys.argv[1])
    write_text(ev, gr, sys.argv[2])
