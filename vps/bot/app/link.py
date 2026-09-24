"""Связь с ПК у ворот.

ПК у ворот сам подключается к VPS по WebSocket (wss://домен/link) и держит
соединение. Белый IP на объекте не нужен. По этому соединению:
  ПК → VPS: hello, notify (уведомление с фото и кнопками), status (снимок
            состояния раз в 30 с), result (ответ на команду);
  VPS → ПК: command (open, status, last, list, add, del, outage_summary).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Awaitable, Callable

from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger("vps.link")


class SiteOffline(Exception):
    pass


class SiteLink:
    def __init__(self):
        self.ws: WebSocket | None = None
        self.info: dict = {}
        self.connected_at = 0.0
        self.disconnected_at = time.time()  # «не на связи» считаем от старта бота
        self.last_status: tuple[float, str] | None = None
        self._pending: dict[str, asyncio.Future] = {}
        # обработчики, задаются ботом
        self.on_notify: Callable[[dict], Awaitable[None]] | None = None
        self.on_connect: Callable[[float], Awaitable[None]] | None = None
        self.on_disconnect: Callable[[], Awaitable[None]] | None = None

    @property
    def online(self) -> bool:
        return self.ws is not None

    async def serve(self, ws: WebSocket):
        await ws.accept()
        if self.ws is not None:
            try:
                await self.ws.close(code=4000, reason="replaced")
            except Exception:
                pass
        was_down_since = self.disconnected_at if self.ws is None else None
        self.ws, self.info = ws, {}
        self.connected_at = time.time()
        log.info("Объект подключился: %s", ws.client)
        if self.on_connect and was_down_since is not None:
            asyncio.create_task(self.on_connect(was_down_since))
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                kind = msg.get("type")
                if kind == "hello":
                    self.info = {k: v for k, v in msg.items() if k != "type"}
                elif kind == "status":
                    self.last_status = (time.time(), msg.get("text", ""))
                elif kind == "result":
                    fut = self._pending.pop(msg.get("id", ""), None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                elif kind == "notify" and self.on_notify:
                    asyncio.create_task(self.on_notify(msg))
        except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
            pass
        finally:
            if self.ws is ws:
                self.ws = None
                self.disconnected_at = time.time()
                log.warning("Объект отключился")
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(SiteOffline())
                self._pending.clear()
                if self.on_disconnect:
                    asyncio.create_task(self.on_disconnect())

    async def request(self, cmd: str, timeout: float = 15, **params) -> str:
        """Отправляет команду на ПК у ворот и возвращает текст ответа."""
        ws = self.ws
        if ws is None:
            raise SiteOffline()
        cmd_id = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = fut
        try:
            await ws.send_text(json.dumps({"type": "command", "id": cmd_id, "cmd": cmd, **params}))
            res = await asyncio.wait_for(fut, timeout)
        except (asyncio.TimeoutError, SiteOffline):
            raise
        except Exception as e:
            raise SiteOffline() from e
        finally:
            self._pending.pop(cmd_id, None)
        return res.get("text", "")
