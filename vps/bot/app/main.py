"""Бот на VPS: приём данных с ПК у ворот (HTTP) и чат-бот Битрикс24."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .bitrix import Bitrix24Bot
from .config import load_config
from .site import SiteState

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vps")

cfg = load_config()
site = SiteState(cfg.data_dir, cfg.site_online_timeout, cfg.op_ttl)
bot = Bitrix24Bot(cfg, site)
started_at = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [asyncio.create_task(bot.poll_loop()), asyncio.create_task(bot.watch_site())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="GS LPR VPS bot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def site_auth(request: Request):
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    if not token or not hmac.compare_digest(token.encode(), cfg.site_token.encode()):
        log.warning("Запрос с неверным SITE_TOKEN: %s", request.client.host if request.client else "?")
        raise HTTPException(401, "bad token")


@app.post("/api/site/sync", dependencies=[Depends(site_auth)])
async def site_sync(request: Request):
    """ПК у ворот: состояние, копия списка номеров, события, результаты правок → правки для применения."""
    payload = await request.json()
    ops = await bot.on_site_sync(payload)
    return {"ops": ops, "time": time.time()}


@app.post("/api/site/notify", dependencies=[Depends(site_auth)])
async def site_notify(request: Request):
    """ПК у ворот: уведомление (текст, фото, кнопки) для чата в Битрикс24."""
    payload = await request.json()
    asyncio.create_task(bot.on_site_notify(payload))
    return {"ok": True}


@app.get("/healthz")
async def healthz():
    """Для монитора Uptime Kuma (тип HTTP — ключевое слово "bot_ok")."""
    ok = bot.ready or not bot.enabled
    return JSONResponse({
        "status": "bot_ok" if ok else "bot_error",
        "bitrix24": {"enabled": bot.enabled, "ready": bot.ready, "error": bot.b24_error},
        "site": {"online": site.online, "last_sync": site.last_sync or None, "pending_ops": len(site.ops)},
        "uptime": round(time.time() - started_at),
    }, status_code=200 if ok else 503)
