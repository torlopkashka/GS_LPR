"""Бот на VPS: точка подключения ПК у ворот (/link) и чат-бот Битрикс24."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse

from .bitrix import Bitrix24Bot
from .config import load_config
from .link import SiteLink

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vps")

cfg = load_config()
link = SiteLink()
bot = Bitrix24Bot(cfg, link)
started_at = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [asyncio.create_task(bot.poll_loop()), asyncio.create_task(bot.watch_site())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="GS LPR VPS bot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.websocket("/link")
async def site_link(ws: WebSocket):
    auth = ws.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    if not token or not hmac.compare_digest(token.encode(), cfg.link_token.encode()):
        log.warning("Подключение с неверным LINK_TOKEN: %s", ws.client)
        await ws.close(code=4401)
        return
    await link.serve(ws)


@app.get("/healthz")
async def healthz():
    """Для монитора Uptime Kuma (тип HTTP, ключевое слово "bot_ok")."""
    ok = bot.ready or not bot.enabled
    return JSONResponse({
        "status": "bot_ok" if ok else "bot_error",
        "bitrix24": {"enabled": bot.enabled, "ready": bot.ready, "error": bot.b24_error},
        "site": {"online": link.online, "since": link.connected_at if link.online else link.disconnected_at,
                 "info": link.info},
        "uptime": round(time.time() - started_at),
    }, status_code=200 if ok else 503)
