"""Aufzeichnung einer Sitzung im Datenstrom (Live- und gemischter Betrieb).

Beide Geräteströme werden während der Läufe geschrieben – das Live-Gerät aus seinen Rohdaten (T2-Records der
HydraHarp, Record-Header des ADQ32), das simulierte bzw. wiedergegebene Gerät aus seinem Datenstrom – sodass jede
Datei die Betriebsart ihres Geräts trägt (``hhd.io.modes``). Ergebnis ist eine normale Sitzung (``hhd.io.session``):
PTU-Dateien, ``digitizer.h5`` (Schema 1.2) und ``session.json``; eine Wahrheitsdatei gibt es hier nie.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .. import __version__
from . import modes as M
from . import ptu as P
from . import schema as S
from .stream_writer import DigitizerStreamWriter, PtuStreamWriter


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class SessionRecorder:
    """Schreibt eine Sitzung aus Datenströmen (Live-/gemischter Betrieb, Datenstrom-Simulator)."""

    def __init__(self, out_dir: Path, modes: dict, meta: dict, device_attrs: dict, hardware: dict | None = None,
                 extra_manifest: dict | None = None):
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.modes = M.check(modes)
        self.meta = meta
        self.hardware = hardware or {}
        self.extra = extra_manifest or {}
        root = dict(simulated=self.simulated, software=f"hhd {__version__}",
                    created=time.strftime("%Y-%m-%dT%H:%M:%S%z"), session_id=meta["session_id"],
                    campaign_id=meta["campaign_id"], calibration_id=meta["calibration_id"], **M.h5_attrs(self.modes))
        dev = dict(device_attrs) | dict(device_mode=self.modes["digitizer"])
        self.h5 = DigitizerStreamWriter(self.out / "digitizer.h5", root, dev)
        self.runs: list[dict] = []
        self._ptu: PtuStreamWriter | None = None
        self._run = None

    @property
    def simulated(self) -> bool:
        return "live" not in self.modes.values() and self.modes != {"harp": "replay", "digitizer": "replay"}

    def update_device(self, attrs: dict) -> None:
        self.h5.update_device(attrs)

    def begin_run(self, run) -> None:
        """``run``: ``RunSpec`` (block_id, kind, duration_s, index, run_id, extra[\"attrs\"])."""
        a = dict(run.extra.get("attrs", {}))
        a.update(session_id=self.meta["session_id"], campaign_id=self.meta["campaign_id"],
                 calibration_id=self.meta["calibration_id"], device_mode_harp=self.modes["harp"],
                 device_mode_digitizer=self.modes["digitizer"])
        a.setdefault("ptu_file", f"harp_{run.block_id}.ptu")
        self._attrs = a
        hw = self.hardware.get("harp", {})
        hw_type = hw.get("model") or "HydraHarp 500"
        if self.modes["harp"] == "sim":
            hw_type += " (SIMULATED)"
        tags = {"UsrHHD_BlockId": (P.TY_ANSI, run.block_id), "UsrHHD_RunId": (P.TY_ANSI, run.run_id),
                "UsrHHD_RunIndex": (P.TY_ANSI, str(run.index)), "UsrHHD_SessionId": (P.TY_ANSI, self.meta["session_id"]),
                "UsrHHD_CampaignId": (P.TY_ANSI, self.meta["campaign_id"]),
                "UsrHHD_ComparisonRole": (P.TY_ANSI, str(a.get("comparison_role", ""))),
                "UsrHHD_DatasetType": (P.TY_ANSI, str(a.get("dataset_type", ""))),
                "UsrHHD_ChannelRoles": (P.TY_ANSI, str(a.get("harp_channel_roles", "{}"))),
                "UsrHHD_MarkerRoles": (P.TY_ANSI, str(a.get("harp_marker_roles", "{}"))),
                "UsrHHD_CalibrationId": (P.TY_ANSI, self.meta["calibration_id"]), **M.ptu_tags(self.modes)}
        if hw.get("serial"):
            tags["HW_SerialNo"] = (P.TY_ANSI, str(hw["serial"]))
        if hw.get("library_version"):
            tags["UsrHHD_HH500LibVersion"] = (P.TY_ANSI, str(hw["library_version"]))
        self._ptu = PtuStreamWriter(self.out / a["ptu_file"], comment=f"Heralded Homodyne Mobile – Block {run.block_id} "
                                    f"({a.get('dataset_type', '')}), Betriebsart Harp {self.modes['harp']}",
                                    hw_type=hw_type, acquisition_time_ms=int(round(run.duration_s * 1000)), extra_tags=tags)
        self.h5.begin_block(run.block_id, a)
        self._run = run
        self._t_wall = time.time()

    def feed(self, h, d) -> None:
        """``h`` mit ``records`` (T2, uint32), ``d`` mit ``data`` und ``raw`` (siehe ``hhd.server.devices``)."""
        if h is not None and h.records is not None and self._ptu is not None:
            self._ptu.write(h.records)
        if d is not None and d.raw is not None and self.h5.g is not None:
            r = d.raw
            self.h5.append(d.data, r["timestamp"], r["record_start"], r["trigger_source"], r.get("markers"),
                           r.get("data_b"), r.get("marker_b"), aux=r.get("aux"))

    def end_run(self, complete: bool = True) -> None:
        run, a = self._run, self._attrs
        n_h = self._ptu.close() if self._ptu else 0
        n_d = self.h5.end_block()
        self._ptu, self._run = None, None
        if not complete:                                    # abgebrochener Lauf: nicht in die Sitzung übernehmen
            del self.h5.f["blocks"][run.block_id]
            (self.out / a["ptu_file"]).unlink(missing_ok=True)
            self.h5.block_ids.remove(run.block_id)
            return
        self.runs.append(dict(run_id=run.run_id, id=run.block_id, index=run.index, kind=run.kind,
                              dataset_type=a.get("dataset_type", ""), comparison_role=a.get("comparison_role", ""),
                              t_start_s=float(a.get("t_start_s", 0.0)), duration_s=run.duration_s,
                              ptu_file=a["ptu_file"], n_records=int(n_d), n_harp_records=int(n_h),
                              t_wall_start=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self._t_wall))))

    def close(self) -> dict:
        if self._run is not None:
            self.end_run(complete=False)
        vac = [r["run_id"] for r in self.runs if r["dataset_type"] == "vacuum"]
        self.h5.close(dict(calibration_id=self.meta["calibration_id"], mode="shared",
                           runs=[r["run_id"] for r in self.runs],
                           vacuum_blocks=[r["id"] for r in self.runs if r["dataset_type"] == "vacuum"],
                           electronic_blocks=[r["id"] for r in self.runs if r["dataset_type"] == "electronic"]))
        files = {"digitizer.h5": "digitizer"} | {r["ptu_file"]: "harp" for r in self.runs}
        man = dict(format="hhd-session", schema_version=S.SCHEMA_VERSION, software=f"hhd {__version__}", simulated=self.simulated,
                   device_modes=dict(self.modes), acquisition_mode=M.acquisition_mode(self.modes),
                   session_id=self.meta["session_id"], campaign_id=self.meta["campaign_id"],
                   calibration=dict(calibration_id=self.meta["calibration_id"], mode="shared", vacuum_runs=vac),
                   hardware=self.hardware, runs=self.runs,
                   files={k: dict(role=v, sha256=_sha256(self.out / k)) for k, v in files.items()}) | self.extra
        (self.out / "session.json").write_text(json.dumps(man, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        return man
