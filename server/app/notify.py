"""Уведомления в Telegram с кнопками «Открыть» и «Добавить в список»."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

from .config import TelegramConfig
from .plates import display

log = logging.getLogger("lpr.telegram")

TITLES = {
    "granted": "✅ Проезд разрешён",
    "denied": "⛔ Неизвестный номер",
    "logged": "📷 Номер распознан",
    "error": "⚠️ Не удалось открыть ворота",
}


class TelegramNotifier:
    def __init__(self, cfg: TelegramConfig, cameras: dict):
        self.cfg = cfg
        self.cameras = cameras
        self.api = f"https://api.telegram.org/bot{cfg.token}"
        self.client = httpx.AsyncClient(timeout=40)
        self.controller = None  # AccessController, задаётся в main
        self.db = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.token and self.cfg.chat_ids)

    async def _call(self, method: str, **kw):
        r = await self.client.post(f"{self.api}/{method}", **kw)
        data = r.json()
        if not data.get("ok"):
            log.warning("Telegram %s: %s", method, data.get("description"))
        return data

    async def send_text(self, text: str):
        for chat in self.cfg.chat_ids:
            try:
                await self._call("sendMessage", data={"chat_id": chat, "text": text})
            except Exception as e:
                log.warning("Telegram: %s", e)

    async def event(self, ev: dict, snapshot: Path | None):
        if not self.enabled or ev is None:
            return
        d = ev["decision"]
        if (d == "granted" and not self.cfg.notify_granted) or (d == "denied" and not self.cfg.notify_denied):
            return
        cam = self.cameras.get(ev["camera"])
        lines = [f"{TITLES.get(d, d)}: {display(ev['plate'])}"]
        if ev.get("owner"):
            lines.append(f"Владелец: {ev['owner']}")
        lines.append(f"Камера: {cam.name if cam else ev['camera']}")
        if ev.get("detail") and d != "denied":
            lines.append(ev["detail"])
        lines.append(time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(ev["ts"])))
        caption = "\n".join(lines)
        markup = None
        if d in ("denied", "logged", "error") and self.cfg.interactive:
            markup = json.dumps({"inline_keyboard": [[
                {"text": "🔓 Открыть ворота", "callback_data": f"open:{ev['id']}"},
                {"text": "➕ В список", "callback_data": f"add:{ev['id']}"},
            ]]})
        for chat in self.cfg.chat_ids:
            data = {"chat_id": chat, "caption": caption}
            if markup:
                data["reply_markup"] = markup
            try:
                if snapshot and snapshot.exists():
                    with open(snapshot, "rb") as f:
                        await self._call("sendPhoto", data=data, files={"photo": f})
                else:
                    data["text"] = data.pop("caption")
                    await self._call("sendMessage", data=data)
            except Exception as e:
                log.warning("Telegram: %s", e)

    # --- приём команд ---------------------------------------------------------
    async def poll_loop(self):
        if not self.enabled or not self.cfg.interactive:
            return
        offset = 0
        while True:
            try:
                data = await self._call("getUpdates", data={"offset": offset, "timeout": 30})
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    await self._handle(upd)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Telegram polling: %s", e)
                await asyncio.sleep(5)

    async def _handle(self, upd: dict):
        if "callback_query" in upd:
            q = upd["callback_query"]
            chat = q.get("message", {}).get("chat", {}).get("id")
            if chat not in self.cfg.chat_ids:
                return
            who = q["from"].get("first_name", "") or q["from"].get("username", "")
            action, _, ev_id = q.get("data", "").partition(":")
            ev = self.db.get_event(int(ev_id)) if ev_id.isdigit() else None
            answer = "Неизвестная команда"
            if action == "open":
                ok, detail = await self.controller.open_gate(f"Telegram ({who})", force=True)
                answer = "Ворота открываются" if ok else f"Ошибка: {detail}"
                if ok and ev:
                    self.db.update_event(ev["id"], decision="manual", detail=f"открыто из Telegram: {who}")
            elif action == "add" and ev and ev["plate"]:
                self.db.add_plate(ev["plate"], owner="", note=f"добавлен из Telegram ({who})")
                answer = f"{display(ev['plate'])} добавлен в список"
            await self._call("answerCallbackQuery", data={"callback_query_id": q["id"], "text": answer})
            await self.send_text(f"{answer} — {who}")
        elif "message" in upd:
            msg = upd["message"]
            if msg.get("chat", {}).get("id") not in self.cfg.chat_ids:
                return
            text = (msg.get("text") or "").strip().split("@")[0]
            who = msg.get("from", {}).get("first_name", "")
            if text == "/open":
                ok, detail = await self.controller.open_gate(f"Telegram ({who})", force=True)
                await self.send_text("Ворота открываются" if ok else f"Ошибка: {detail}")
            elif text == "/status":
                hub = self.controller.hub
                await self.send_text("Агент ворот: " + ("на связи" if hub.online else "НЕ на связи"))
            elif text in ("/start", "/help"):
                await self.send_text("/open — открыть ворота\n/status — состояние")
