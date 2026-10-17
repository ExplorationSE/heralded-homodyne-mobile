"""FastAPI-Anwendung des HHD-Mess-Servers (Muster ``harp/server.py`` in GX).

* ``/``           Web-App (statisch, ``hhd/server/web``), mobil tauglich, deutsch;
* ``/ws``         WebSocket: Status/Live-Vorschau (``type: status``, ``status_hz``), Befehle
                  ``{type: "command", cmd, params, id}`` → ``{type: "ack", id, ok, msg, …}``;
* ``/api/…``      REST (lesend): health, status, sessions, Ergebnis/Kurzfassung/Bericht/Live-Aufzeichnung, Protokoll;
* Zugangscode (``token`` in ``server.toml``): ``?token=`` oder Anmeldeseite → HttpOnly-Cookie; WebSocket nur mit
  passender Origin; Sicherheits-Header auf allen Antworten.

Die Web-App spricht **nur** mit diesem Server; Geräte, Dateien und Auswertung bleiben auf dem Mess-PC.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .control import COOKIE, Auth, CommandError, client_ip, is_https, is_local, login_page, origin_ok
from .service import MeasurementService, ServerConfig

log = logging.getLogger("hhd.server")
WEB = Path(__file__).parent / "web"


def _safe_next(n: str | None) -> str:
    return n if n and n.startswith("/") and not n.startswith("//") else "/"


def create_app(cfg: ServerConfig, root: Path | None = None, service: MeasurementService | None = None) -> FastAPI:
    svc = service or MeasurementService(cfg, root)
    auth = Auth(cfg.token)
    app = FastAPI(title="HHD Mess-Server", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.svc = svc
    clients: dict[str, WebSocket] = {}
    loop_ref: dict = {}

    def _authed(request: Request) -> bool:
        return not auth.enabled or auth.check_cookie(request.cookies.get(COOKIE))

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        open_paths = ("/login", "/api/health", "/manifest.webmanifest", "/icon.svg")
        if auth.enabled and not path.startswith(open_paths) and not _authed(request):
            ip = client_ip(request.headers, request.client.host if request.client else "?")
            tok = request.query_params.get("token")
            if tok is not None and not auth.blocked(ip):
                if auth.check_code(tok):
                    q = "&".join(f"{k}={v}" for k, v in request.query_params.items() if k != "token")
                    resp = RedirectResponse(path + (f"?{q}" if q else ""), status_code=303)
                    resp.set_cookie(COOKIE, auth.session_value(), httponly=True, samesite="lax",
                                    secure=is_https(request.url.scheme, request.headers), path="/")
                    return resp
                auth.record_fail(ip)
            if path.startswith("/api/"):
                resp = JSONResponse({"detail": "nicht angemeldet"}, status_code=401)
            else:
                resp = RedirectResponse(f"/login?next={path}", status_code=303)
        else:
            resp = await call_next(request)
        resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Content-Security-Policy",
                                "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                                "connect-src 'self' ws: wss:; frame-ancestors 'none'")
        return resp

    @app.get("/login")
    async def login_get(next: str | None = None):
        return HTMLResponse(login_page("", _safe_next(next)), headers={"Cache-Control": "no-store"})

    @app.post("/login")
    async def login_post(request: Request, code: str = Form(""), next: str = Form("/")):
        ip = client_ip(request.headers, request.client.host if request.client else "?")
        if auth.blocked(ip):
            return HTMLResponse(login_page("Zu viele Fehlversuche – bitte später erneut.", _safe_next(next)), status_code=429)
        if not auth.check_code(code):
            auth.record_fail(ip)
            return HTMLResponse(login_page("Code falsch.", _safe_next(next)), status_code=401)
        resp = RedirectResponse(_safe_next(next), status_code=303)
        resp.set_cookie(COOKIE, auth.session_value(), httponly=True, samesite="lax",
                        secure=is_https(request.url.scheme, request.headers), path="/")
        return resp

    @app.get("/logout")
    async def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    # ---------------------------------------------------------------- REST
    def _err(e: Exception):
        raise HTTPException(404, str(e))

    @app.get("/api/health")
    async def health():
        return {"ok": True, "version": svc.status()["version"], "mode": cfg.device_mode, "auth": auth.enabled}

    @app.get("/api/status")
    async def status(request: Request):
        st = svc.status()
        st["you_local"] = is_local(request.headers, request.client.host if request.client else "?")
        return st

    @app.get("/api/sessions")
    async def sessions():
        return svc.sessions()

    @app.get("/api/sessions/{sid}/result")
    async def result(sid: str):
        try:
            return svc.result(sid)
        except CommandError as e:
            _err(e)

    @app.get("/api/sessions/{sid}/summary")
    async def summary(sid: str):
        try:
            return svc.summary(sid)
        except CommandError as e:
            _err(e)

    @app.get("/api/sessions/{sid}/report")
    async def report(sid: str):
        try:
            return PlainTextResponse(svc.report(sid), media_type="text/markdown; charset=utf-8")
        except CommandError as e:
            _err(e)

    @app.get("/api/sessions/{sid}/live")
    async def live_record(sid: str):
        try:
            return svc.live_record(sid)
        except CommandError as e:
            _err(e)

    @app.get("/api/log")
    async def cmdlog(n: int = Query(100, ge=1, le=1000)):
        return svc.log.tail(n)

    # ---------------------------------------------------------------- WebSocket
    async def broadcast(msg: dict) -> None:
        data = json.dumps(msg, ensure_ascii=False, default=str)
        for cid, ws in list(clients.items()):
            try:
                await ws.send_text(data)
            except Exception:  # noqa: BLE001
                clients.pop(cid, None)

    def _kick() -> None:                         # aus Threads: sofortiger Status
        lp = loop_ref.get("loop")
        if lp is not None and clients:
            lp.call_soon_threadsafe(lambda: asyncio.ensure_future(broadcast(svc.status())))

    svc.listeners.append(_kick)

    async def ticker():
        while True:
            await asyncio.sleep(1.0 / max(cfg.status_hz, 0.2))
            if clients:
                await broadcast(svc.status())

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        loop_ref["loop"] = asyncio.get_running_loop()
        loop_ref["task"] = asyncio.create_task(ticker())
        try:
            yield
        finally:
            loop_ref["task"].cancel()
            svc.close()

    app.router.lifespan_context = lifespan

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket, token: str | None = Query(None)):
        peer = ws.client.host if ws.client else "?"
        ip = client_ip(ws.headers, peer)
        if auth.enabled:
            ok = auth.check_cookie(ws.cookies.get(COOKIE)) or (token is not None and not auth.blocked(ip)
                                                                and auth.check_code(token))
            if not ok:
                if token is not None:
                    auth.record_fail(ip)
                await ws.close(code=4401)
                return
        if not origin_ok(ws.headers.get("origin"), ws.headers.get("host"), ws.headers.get("x-forwarded-host")):
            await ws.close(code=4403)
            return
        await ws.accept()
        cid = uuid.uuid4().hex[:10]
        local = is_local(ws.headers, peer)
        clients[cid] = ws
        await ws.send_text(json.dumps(dict(type="hello", client=cid, local=local, addr=ip), ensure_ascii=False))
        await ws.send_text(json.dumps(svc.status() | {"you_local": local}, ensure_ascii=False, default=str))
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    m = json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send_text(json.dumps(dict(type="ack", ok=False, msg="kein JSON")))
                    continue
                if not isinstance(m, dict) or m.get("type") != "command":
                    continue
                res = await asyncio.to_thread(svc.handle, cid, ip, local, str(m.get("cmd")), m.get("params"),
                                              str(m.get("name", ""))[:40])
                await ws.send_text(json.dumps(dict(type="ack", id=m.get("id"), cmd=m.get("cmd"), **res),
                                              ensure_ascii=False, default=str))
                await broadcast(svc.status())
        except WebSocketDisconnect:
            pass
        finally:
            clients.pop(cid, None)
            svc.lock.release(cid)

    @app.get("/")
    async def index():
        return Response((WEB / "index.html").read_bytes(), media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store"})

    app.mount("/", StaticFiles(directory=WEB), name="web")
    return app
