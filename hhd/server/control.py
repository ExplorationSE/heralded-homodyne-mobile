"""Steuersperre, Befehlsprotokoll, Zugangsschutz und Parameterrechte (ED-8b) des HHD-Mess-Servers.

Muster: ``harp/control.py`` und ``harp/auth.py`` in GX (nur gelesen, nicht verändert):

* ``ControlLock`` – nur ein Client steuert; die Sperre verfällt bei Trennung oder nach Inaktivität;
* ``CommandLog`` – jeder Befehl als JSON-Zeile in ``logs/commands.jsonl`` (auch abgelehnte);
* ``Auth`` – Zugangscode (``?token=`` oder Anmeldeseite → HttpOnly-Cookie), Fehlversuche je IP begrenzt;
* ``ParamPolicy`` – ED-8b: Start/Stopp, Messablauf und Anzeige von überall (mit Steuersperre); kritische
  Geräteparameter nur am Mess-PC (localhost ohne Tunnel-Header) oder aus der Ferne mit ausdrücklicher
  Bestätigung (zweistufig, Bestätigungscode 120 s gültig); Triggerausgänge nie aus der Ferne.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

COOKIE = "hhd_session"
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
MAX_FAIL_PER_IP = 8
MAX_FAIL_GLOBAL = 100
WINDOW_S = 600
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
CONFIRM_TTL_S = 120.0
PROXY_HEADERS = ("cf-connecting-ip", "x-forwarded-for", "x-real-ip", "forwarded", "cf-ray")

ALLOWED_CMDS = {"acquire_control", "release_control", "start_session", "stop_session", "set_display",
                "set_device", "analyze", "clear_warnings", "continue_run",
                "check_replay_dir", "set_replay_dir", "clear_replay_dir", "browse_dir"}


class CommandError(ValueError):
    pass


class ControlLock:
    """Nur ein Client darf steuern. Sperre verfällt bei Trennung (``release``) oder nach Inaktivität."""

    def __init__(self, timeout_s: float = 300.0) -> None:
        self.timeout_s = timeout_s
        self.holder: str | None = None
        self.holder_name: str = ""
        self.last = 0.0
        self._lock = threading.Lock()

    def _expired(self) -> bool:
        return self.holder is not None and (time.monotonic() - self.last) > self.timeout_s

    def acquire(self, client: str, name: str = "") -> bool:
        with self._lock:
            if self.holder in (None, client) or self._expired():
                self.holder, self.holder_name, self.last = client, name, time.monotonic()
                return True
            return False

    def check(self, client: str) -> bool:
        with self._lock:
            if self._expired():
                self.holder = None
            if self.holder == client:
                self.last = time.monotonic()
                return True
            return False

    def release(self, client: str) -> None:
        with self._lock:
            if self.holder == client:
                self.holder = None
                self.holder_name = ""

    def state(self) -> dict:
        with self._lock:
            if self._expired():
                self.holder = None
            return dict(holder=self.holder, name=self.holder_name, timeout_s=self.timeout_s)


class CommandLog:
    """Protokolliert jeden Befehl als JSON-Zeile (``logs/commands.jsonl``)."""

    def __init__(self, log_dir: Path) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        self.path = log_dir / "commands.jsonl"
        self._lock = threading.Lock()

    def write(self, client: str, addr: str, cmd: str, params: object, ok: bool, msg: str = "",
              local: bool | None = None) -> dict:
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "client": client, "addr": addr, "local": local,
                 "cmd": cmd, "params": params, "ok": ok, "msg": msg}
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return entry

    def tail(self, n: int = 100) -> list[dict]:
        if not self.path.exists():
            return []
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
        return out


def generate_code(n: int = 10) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def normalize(code: str | None) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


class Auth:
    def __init__(self, token: str) -> None:
        self.code = normalize(token)
        self.enabled = bool(self.code)
        self._salt = secrets.token_bytes(16)
        self._fails: dict[str, deque] = defaultdict(deque)
        self._gfails: deque = deque()

    def session_value(self) -> str:
        return hmac.new(self._salt, self.code.encode(), hashlib.sha256).hexdigest()

    def check_code(self, code: str | None) -> bool:
        return self.enabled and hmac.compare_digest(normalize(code).encode(), self.code.encode())

    def check_cookie(self, value: str | None) -> bool:
        return bool(value) and hmac.compare_digest(value.encode(), self.session_value().encode())

    def _prune(self, dq: deque) -> None:
        now = time.monotonic()
        while dq and now - dq[0] > WINDOW_S:
            dq.popleft()

    def blocked(self, ip: str) -> bool:
        self._prune(self._fails[ip]); self._prune(self._gfails)
        return len(self._fails[ip]) >= MAX_FAIL_PER_IP or len(self._gfails) >= MAX_FAIL_GLOBAL

    def record_fail(self, ip: str) -> None:
        now = time.monotonic()
        self._fails[ip].append(now); self._gfails.append(now)


def client_ip(headers, peer: str) -> str:
    """Client-IP; CF-Connecting-IP nur von localhost (dem lokal laufenden Tunnel) akzeptieren."""
    if peer in LOOPBACK:
        cf = headers.get("cf-connecting-ip")
        if cf:
            return cf.strip()
    return peer


def is_local(headers, peer: str) -> bool:
    """Mess-PC = Verbindung von localhost **ohne** Proxy-/Tunnel-Header (cloudflared verbindet ebenfalls von
    localhost; solche Verbindungen gelten als entfernt)."""
    if peer not in LOOPBACK:
        return False
    return not any(headers.get(h) for h in PROXY_HEADERS)


def origin_ok(origin: str | None, host: str | None, forwarded_host: str | None = None) -> bool:
    if not origin:
        return True
    if origin == "null":
        return False
    o = _hostport(origin.split("://", 1)[-1])
    return o in {_hostport(h) for h in (host, forwarded_host) if h}


def _hostport(v: str) -> str:
    v = v.strip().lower().rstrip("/")
    for s in (":443", ":80"):
        if v.endswith(s):
            v = v[: -len(s)]
    return v


def is_https(scheme: str, headers) -> bool:
    if scheme in ("https", "wss"):
        return True
    return (headers.get("x-forwarded-proto") or "").lower().split(",")[0].strip() == "https"


SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,80}$")


def safe_session_dir(data_dir: Path, sid: str) -> Path:
    """Nur Sitzungsnamen aus dem Datenordner zulassen (kein Pfad-Traversal)."""
    if not isinstance(sid, str) or not SESSION_RE.match(sid) or ".." in sid:
        raise CommandError("ungültige Sitzungs-ID")
    p = (data_dir / sid).resolve()
    if p.parent != data_dir.resolve() or not (p / "session.json").exists():
        raise CommandError(f"Sitzung nicht gefunden: {sid}")
    return p


class ParamPolicy:
    """ED-8b-Rechte für Geräteparameter mit zweistufiger Bestätigung aus der Ferne."""

    def __init__(self) -> None:
        self._pending: dict[str, tuple[float, str, str, dict]] = {}
        self._lock = threading.Lock()

    def check(self, device, params: dict, local: bool, client: str, confirm: str | None) -> tuple[str, str]:
        """Rückgabe (Ergebnis, Info): ``ok`` (anwenden), ``confirm`` (Bestätigung nötig; Info = Code)
        oder wirft ``CommandError``."""
        if not isinstance(params, dict) or not params:
            raise CommandError("'params' muss ein nicht-leeres Objekt sein")
        specs = {}
        for k in params:
            try:
                specs[k] = device.spec(k)
            except KeyError:
                raise CommandError(f"unbekannter Parameter {device.kind}.{k}") from None
            try:
                specs[k].validate(params[k])
            except ValueError as e:
                raise CommandError(str(e)) from None
        if local:
            return "ok", "lokal (Mess-PC)"
        lo = [k for k, s in specs.items() if s.local_only]
        if lo:
            raise CommandError(f"{', '.join(lo)}: nur am Mess-PC schaltbar (ED-8b, Triggerausgänge nie aus der Ferne)")
        crit = sorted(k for k, s in specs.items() if s.critical)
        if not crit:
            return "ok", "unkritisch"
        key = json.dumps([device.kind, params], sort_keys=True, default=str)
        now = time.monotonic()
        with self._lock:
            for c, (t, *_r) in list(self._pending.items()):
                if now - t > CONFIRM_TTL_S:
                    del self._pending[c]
            if confirm:
                ent = self._pending.pop(str(confirm), None)
                if ent is None:
                    raise CommandError("Bestätigung ungültig oder abgelaufen")
                _, cl, k2, _ = ent
                if cl != client or k2 != key:
                    raise CommandError("Bestätigung passt nicht zu dieser Änderung")
                return "ok", "aus der Ferne bestätigt"
            code = secrets.token_hex(4)
            self._pending[code] = (now, client, key, params)
        return "confirm", code

    def check_action(self, what: str, detail: dict, local: bool, client: str, confirm: str | None) -> tuple[str, str]:
        """Zweistufige Bestätigung einer Aktion aus der Ferne (z. B. Start einer Sitzung mit Live-Geräten)."""
        if local:
            return "ok", "lokal (Mess-PC)"
        key = json.dumps(["action", what, detail], sort_keys=True, default=str)
        now = time.monotonic()
        with self._lock:
            for c, (t, *_r) in list(self._pending.items()):
                if now - t > CONFIRM_TTL_S:
                    del self._pending[c]
            if confirm:
                ent = self._pending.pop(str(confirm), None)
                if ent is None:
                    raise CommandError("Bestätigung ungültig oder abgelaufen")
                _, cl, k2, _ = ent
                if cl != client or k2 != key:
                    raise CommandError("Bestätigung passt nicht zu dieser Aktion")
                return "ok", "aus der Ferne bestätigt"
            code = secrets.token_hex(4)
            self._pending[code] = (now, client, key, detail)
        return "confirm", code


def login_page(error: str = "", next_url: str = "/") -> str:
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>Heralded Homodyne Mobile – Anmeldung</title><style>
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0f172a;color:#e2e8f0;
font:16px system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
form{{background:#111c33;border:1px solid #22304f;border-radius:16px;padding:24px;width:min(340px,90vw)}}
h1{{font-size:20px;margin:0 0 6px}}p{{color:#94a3b8;font-size:14px;margin:0 0 16px}}
input{{width:100%;box-sizing:border-box;font-size:22px;letter-spacing:3px;text-transform:uppercase;padding:12px;
border-radius:10px;border:1px solid #22304f;background:#0b1222;color:#e2e8f0;text-align:center}}
button{{width:100%;margin-top:14px;padding:13px;font-size:17px;border:0;border-radius:10px;background:#0e7490;color:#fff}}
.err{{color:#fecaca;margin-top:12px}}</style></head><body>
<form method="post" action="/login"><h1>Heralded Homodyne Mobile</h1><p>Bitte Zugangscode eingeben.</p>
<input name="code" autocomplete="one-time-code" autocapitalize="characters" autocorrect="off" spellcheck="false" autofocus required>
<input type="hidden" name="next" value="{html.escape(next_url)}">
<button type="submit">Anmelden</button>{err}</form></body></html>"""
