"""Веб-приложение: интерфейс управления, API и точка подключения агента ворот."""

from __future__ import annotations

import asyncio
import csv
import hmac
import io
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .config import load_config
from .db import Database
from .gate import AccessController, AgentHub
from .vpssync import VpsSync
from .plates import display, normalize
from .recognizer import CameraWorker

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("lpr")

BASE = Path(__file__).parent
cfg = load_config()
db = Database(cfg.data_dir / "lpr.db")
hub = AgentHub()
cams_by_id = {c.id: c for c in cfg.cameras}
workers: dict[str, CameraWorker] = {}
vps = VpsSync(cfg, db, hub, workers, cams_by_id)
controller = AccessController(cfg, db, hub, vps if vps.enabled else None)

DECISIONS = {
    "granted": "Открыто",
    "denied": "Отказ",
    "manual": "Открыто вручную",
    "logged": "Распознан",
    "error": "Ошибка открытия",
    "system": "Система",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    controller.bind_loop(asyncio.get_running_loop())
    for cam in cfg.cameras:
        if cam.enabled:
            w = CameraWorker(cam, cfg.recognition, controller.on_camera_event)
            workers[cam.id] = w
            w.start()
    tasks = [asyncio.create_task(t) for t in (controller.cleanup_loop(), vps.run(), vps.monitor_loop())]
    log.info("Запущено камер: %d", len(workers))
    yield
    for t in tasks:
        t.cancel()
    for w in workers.values():
        w.stop()


app = FastAPI(title="GS LPR", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=cfg.secret_key, same_site="lax",
                   https_only=os.environ.get("COOKIE_SECURE", "0") == "1", max_age=30 * 86400)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.filters["plate"] = display
templates.env.filters["dt"] = lambda ts: time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(ts)) if ts else ""
templates.env.globals["DECISIONS"] = DECISIONS
templates.env.globals["cams"] = cams_by_id


# --- авторизация -----------------------------------------------------------------
def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


class NeedLogin(Exception):
    pass


@app.exception_handler(NeedLogin)
async def _need_login(request: Request, exc: NeedLogin):
    return RedirectResponse("/login", status_code=303)


def _token_ok(request: Request) -> bool:
    if not cfg.api_token:
        return False
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else request.query_params.get("token", "")
    return bool(token) and _eq(token, cfg.api_token)


def page_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise NeedLogin()
    return user


def api_user(request: Request) -> str:
    if request.session.get("user"):
        return request.session["user"]
    if _token_ok(request):
        return "api"
    raise HTTPException(401, "Требуется авторизация")


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ok = _eq(username, cfg.admin_user) & _eq(password, cfg.admin_password)
    if not ok:
        log.warning("Неудачный вход с %s", request.client.host if request.client else "?")
        await asyncio.sleep(1.5)
        return templates.TemplateResponse(request, "login.html", {"error": "Неверный логин или пароль"},
                                          status_code=401)
    request.session["user"] = username
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- страницы ----------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = Depends(page_user)):
    return templates.TemplateResponse(request, "index.html", {
        "cameras": cfg.cameras,
        "events": db.list_events(limit=15), "allowed": db.allowed_plates(),
    })


@app.get("/partials/events", response_class=HTMLResponse)
async def events_partial(request: Request, user: str = Depends(page_user)):
    return templates.TemplateResponse(request, "_event_rows.html", {
        "events": db.list_events(limit=15), "allowed": db.allowed_plates(),
    })


@app.get("/plates", response_class=HTMLResponse)
async def plates_page(request: Request, user: str = Depends(page_user), q: str = "", add: str = ""):
    plates = db.list_plates()
    if q:
        nq = normalize(q)
        plates = [p for p in plates if nq in p["plate"] or q.lower() in (p["owner"] + p["note"]).lower()]
    return templates.TemplateResponse(request, "plates.html", {
        "plates": plates, "q": q, "add": add, "today": time.strftime("%Y-%m-%d"),
        "msg": request.query_params.get("msg", ""),
    })


@app.post("/plates")
async def plates_add(user: str = Depends(page_user), plate: str = Form(...), owner: str = Form(""),
                     note: str = Form(""), valid_until: str = Form("")):
    p = normalize(plate)
    if len(p) < 4:
        return RedirectResponse("/plates?msg=Некорректный+номер", status_code=303)
    db.add_plate(p, owner.strip(), note.strip(), valid_until or None)
    log.info("Номер %s добавлен (%s)", p, user)
    return RedirectResponse("/plates", status_code=303)


@app.post("/plates/{plate_id}/edit")
async def plates_edit(plate_id: int, user: str = Depends(page_user), plate: str = Form(...),
                      owner: str = Form(""), note: str = Form(""), valid_until: str = Form("")):
    p = normalize(plate)
    if len(p) >= 4:
        db.update_plate(plate_id, plate=p, owner=owner.strip(), note=note.strip(),
                        valid_until=valid_until or None)
    return RedirectResponse("/plates", status_code=303)


@app.post("/plates/{plate_id}/toggle")
async def plates_toggle(plate_id: int, user: str = Depends(page_user)):
    p = db.get_plate(plate_id)
    if p:
        db.update_plate(plate_id, active=0 if p["active"] else 1)
    return RedirectResponse("/plates", status_code=303)


@app.post("/plates/{plate_id}/delete")
async def plates_delete(plate_id: int, user: str = Depends(page_user)):
    db.delete_plate(plate_id)
    return RedirectResponse("/plates", status_code=303)


@app.get("/plates/export.csv")
async def plates_export(user: str = Depends(page_user)):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["plate", "owner", "note", "active", "valid_until"])
    for p in db.list_plates():
        w.writerow([display(p["plate"]), p["owner"], p["note"], p["active"], p["valid_until"] or ""])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=plates.csv"})


@app.post("/plates/import")
async def plates_import(user: str = Depends(page_user), file: UploadFile = File(...)):
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    delim = ";" if text.count(";") >= text.count(",") else ","
    n = 0
    for row in csv.reader(io.StringIO(text), delimiter=delim):
        if not row or row[0].strip().lower() in ("plate", "номер"):
            continue
        p = normalize(row[0])
        if len(p) < 4:
            continue
        get = lambda i: row[i].strip() if len(row) > i else ""
        db.add_plate(p, get(1), get(2), get(4) or None)
        n += 1
    return RedirectResponse(f"/plates?msg=Импортировано:+{n}", status_code=303)


@app.get("/events", response_class=HTMLResponse)
async def events_page(request: Request, user: str = Depends(page_user), q: str = "",
                      decision: str = "", page: int = 1):
    per = 50
    events = db.list_events(limit=per + 1, offset=(page - 1) * per, plate=normalize(q), decision=decision)
    return templates.TemplateResponse(request, "events.html", {
        "events": events[:per], "q": q, "decision": decision, "page": page,
        "has_next": len(events) > per, "allowed": db.allowed_plates(),
    })


# --- API ---------------------------------------------------------------------------------
@app.get("/api/status")
async def api_status(user: str = Depends(api_user)):
    return {
        "agent": hub.status(),
        "cameras": [w.status() for w in workers.values()],
        "vps": {
            "enabled": vps.enabled,
            "online": vps.online if vps.enabled else None,
            "last_sync": vps.last_ok or None,
            "error": vps.last_error,
            "last_outage": vps.last_outage,
        },
        "uptime": round(time.time() - vps.started_at),
        "time": time.time(),
    }


@app.get("/api/events")
async def api_events(user: str = Depends(api_user), limit: int = 20, after: int = 0):
    events = [e for e in db.list_events(limit=min(limit, 200)) if e["id"] > after]
    for e in events:
        e["plate_display"] = display(e["plate"])
        e["decision_text"] = DECISIONS.get(e["decision"], e["decision"])
        e["camera_name"] = cams_by_id[e["camera"]].name if e["camera"] in cams_by_id else e["camera"]
    return events


@app.get("/api/plates")
async def api_plates(user: str = Depends(api_user)):
    return db.list_plates()


@app.post("/api/plates")
async def api_plates_add(request: Request, user: str = Depends(api_user)):
    data = await request.json()
    p = normalize(data.get("plate", ""))
    if len(p) < 4:
        raise HTTPException(400, "Некорректный номер")
    db.add_plate(p, data.get("owner", ""), data.get("note", ""), data.get("valid_until"))
    return {"ok": True, "plate": p}


@app.delete("/api/plates/{plate}")
async def api_plates_delete(plate: str, user: str = Depends(api_user)):
    p = normalize(plate)
    for row in db.list_plates():
        if row["plate"] == p:
            db.delete_plate(row["id"])
            return {"ok": True}
    raise HTTPException(404, "Номер не найден")


@app.get("/api/cameras/{cam_id}/preview.jpg")
async def camera_preview(cam_id: str, user: str = Depends(api_user)):
    w = workers.get(cam_id)
    if not w:
        raise HTTPException(404)
    jpg = await asyncio.to_thread(w.preview_jpeg)
    if jpg is None:
        raise HTTPException(503, "Нет кадра")
    return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/media/{path:path}")
async def media(path: str, user: str = Depends(api_user)):
    root = (cfg.data_dir / "snapshots").resolve()
    f = (root / path).resolve()
    if not f.is_relative_to(root) or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# --- агент ворот -------------------------------------------------------------------
@app.websocket("/ws/agent")
async def agent_ws(ws: WebSocket):
    auth = ws.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    if not token or not _eq(token, cfg.agent_token):
        log.warning("Агент с неверным токеном: %s", ws.client)
        await ws.close(code=4401)
        return
    await hub.serve(ws)
