"""Чат-бот Битрикс24 на VPS.

API «Чат-боты 2.0» (imbot.v2) в режиме fetch: бот сам забирает события
методом imbot.v2.Event.get. Авторизация — входящий вебхук с правами imbot
плюс секрет бота (botToken).

Бот только принимает команды и показывает ответы. Данные (номера, журнал,
состояние ворот) живут на ПК у ворот: команды пересылаются туда по SiteLink.
Когда объект не на связи, бот отвечает сразу сам и сообщает об этом в чат.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime

import httpx

from .config import BotConfig
from .link import SiteLink, SiteOffline

log = logging.getLogger("vps.bitrix24")

# Команды бота: имя без «/» -> (заголовок, подсказка к параметрам)
COMMANDS = {
    "open": ("Открыть ворота", ""),
    "status": ("Состояние объекта: сервер, агент ворот, камеры", ""),
    "last": ("Последние проезды", ""),
    "list": ("Разрешённые номера", ""),
    "add": ("Добавить номер", "А123ВС77 Владелец [до 31.12.2026]"),
    "del": ("Удалить номер", "А123ВС77"),
    "help": ("Справка", ""),
}
FORWARDED = {"open", "status", "last", "list", "add", "del"}

HELP = (
    "[B]Кнопки под сообщениями бота:[/B]\n"
    "🔓 Открыть ворота — подать команду на ворота\n"
    "📡 Статус — на связи ли объект, агент ворот и камеры\n"
    "🕘 Последние — последние 10 событий\n"
    "📋 Номера — разрешённые номера\n\n"
    "[B]Команды:[/B]\n"
    "/add А123ВС77 Иванов — добавить номер (владелец необязателен)\n"
    "/add А123ВС77 Гость до 31.12.2026 — временный доступ\n"
    "/del А123ВС77 — удалить номер\n\n"
    "Если объект без интернета, бот сообщит об этом. Ворота при этом продолжают "
    "открываться по номерам и с пульта."
)


def btn(text: str, command: str, params: str = "", color: str = "base") -> dict:
    b = {"TEXT": text, "COMMAND": command, "DISPLAY": "LINE", "BG_COLOR_TOKEN": color}
    if params:
        b["COMMAND_PARAMS"] = params
    return b


MENU = [
    btn("🔓 Открыть ворота", "open", color="primary"),
    btn("📡 Статус", "status"),
    btn("🕘 Последние", "last"),
    btn("📋 Номера", "list"),
]


def fmt_duration(sec: float) -> str:
    sec = int(sec)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d} д {h} ч"
    if h:
        return f"{h} ч {m} мин"
    if m:
        return f"{m} мин"
    return f"{s} с"


def fmt_time(ts: float) -> str:
    lt, now = time.localtime(ts), time.localtime()
    if lt.tm_yday == now.tm_yday and lt.tm_year == now.tm_year:
        return time.strftime("%H:%M", lt)
    return time.strftime("%d.%m %H:%M", lt)


def event_ts(ev: dict) -> float:
    try:
        return datetime.fromisoformat(ev.get("date", "")).timestamp()
    except (TypeError, ValueError):
        return time.time()


class B24Error(Exception):
    def __init__(self, code: str, description: str = ""):
        super().__init__(f"{code}: {description}")
        self.code = code


class Bitrix24Bot:
    def __init__(self, cfg: BotConfig, link: SiteLink):
        self.cfg = cfg
        self.link = link
        self.client = httpx.AsyncClient(timeout=30)
        self.state_file = cfg.data_dir / "bitrix24.json"
        self.state = self._load_state()
        self.bot_id: int | None = None
        self.ready = False
        self.b24_error = ""
        self.offline_alerted = False
        link.on_notify = self.on_site_notify
        link.on_connect = self.on_site_connect

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.webhook_url and self.cfg.bot_token)

    @property
    def dialog_id(self) -> str:
        return self.cfg.dialog_id or self.state.get("dialog_id", "")

    # --- состояние на диске ------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self):
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state), encoding="utf-8")
        tmp.replace(self.state_file)

    # --- REST ----------------------------------------------------------------------
    async def call(self, method: str, **params) -> dict:
        """Сетевые ошибки пробрасываются как httpx.HTTPError, ошибки API — B24Error."""
        if method.startswith("imbot.v2.") and method != "imbot.v2.Bot.register":
            params.setdefault("botToken", self.cfg.bot_token)
            if self.bot_id is not None and method not in ("imbot.v2.Bot.get", "imbot.v2.Bot.list"):
                params.setdefault("botId", self.bot_id)
        url = self.cfg.webhook_url.rstrip("/") + "/" + method
        r = await self.client.post(url, json=params)
        try:
            data = r.json()
        except ValueError:
            r.raise_for_status()
            raise B24Error("BAD_RESPONSE", r.text[:200])
        if "error" in data:
            raise B24Error(data["error"], data.get("error_description", ""))
        return data.get("result", {})

    async def setup(self):
        """Находит или регистрирует бота, его команды и чат для уведомлений."""
        try:
            res = await self.call("imbot.v2.Bot.get", code=self.cfg.bot_code)
            self.bot_id = int(res["bot"]["id"])
        except B24Error as e:
            if e.code != "BOT_NOT_FOUND":
                raise
            res = await self.call("imbot.v2.Bot.register", fields={
                "code": self.cfg.bot_code,
                "botToken": self.cfg.bot_token,
                "type": "bot",
                "eventMode": "fetch",
                "properties": {"name": self.cfg.bot_name, "workPosition": "Распознавание номеров, ворота"},
            })
            self.bot_id = int(res["bot"]["id"])
            log.info("Бот зарегистрирован, id=%s", self.bot_id)

        existing = await self.call("imbot.v2.Command.list")
        have = {c["command"].lstrip("/") for c in existing.get("commands", [])}
        for cmd, (title, params) in COMMANDS.items():
            if cmd in have:
                continue
            fields = {"command": cmd, "title": {"ru": title, "en": title}, "common": False}
            if params:
                fields["params"] = {"ru": params, "en": params}
            await self.call("imbot.v2.Command.register", fields=fields)

        if not self.dialog_id and self.cfg.user_ids:
            res = await self.call("imbot.v2.Chat.add", fields={
                "title": self.cfg.bot_name, "userIds": self.cfg.user_ids,
                "description": "Уведомления о проездах и управление воротами",
            })
            self.state["dialog_id"] = res["chat"]["dialogId"]
            self._save_state()
            log.info("Создан чат для уведомлений: %s", self.dialog_id)
            await self.send(self.dialog_id, "Чат создан. Сюда будут приходить уведомления о проездах.\n\n" + HELP)
        elif self.state.get("dialog_id") and not self.cfg.dialog_id and self.cfg.user_ids:
            # чат создан ботом: добавить сотрудников, появившихся в B24_USER_IDS
            try:
                members = await self.call("imbot.v2.Chat.User.list", dialogId=self.dialog_id, limit=200)
                rows = members if isinstance(members, list) else members.get("users", [])
                have_ids = {int(u["id"]) for u in rows}
                missing = [u for u in self.cfg.user_ids if u not in have_ids]
                if missing:
                    await self.call("imbot.v2.Chat.User.add", dialogId=self.dialog_id, userIds=missing)
                    log.info("В чат добавлены сотрудники: %s", missing)
            except B24Error as e:
                log.warning("Не удалось обновить участников чата: %s", e)
        self.ready, self.b24_error = True, ""
        log.info("Бот Битрикс24 готов (id=%s, чат %s)", self.bot_id, self.dialog_id or "не задан")

    # --- отправка ---------------------------------------------------------------------
    async def send(self, dialog: str, text: str, keyboard: list | None = MENU,
                   photo: tuple[str, str] | None = None) -> bool:
        """photo — (имя файла, содержимое в base64)."""
        try:
            if photo:
                res = await self.call("imbot.v2.File.upload", dialogId=dialog,
                                      fields={"name": photo[0], "content": photo[1], "message": text})
                if keyboard:
                    # File.upload не принимает кнопки: добавляем их к сообщению с фото,
                    # а если не вышло — отдельным сообщением
                    try:
                        await self.call("imbot.v2.Chat.Message.update", messageId=res.get("messageId"),
                                        fields={"keyboard": keyboard})
                    except B24Error:
                        await self.call("imbot.v2.Chat.Message.send", dialogId=dialog,
                                        fields={"message": "Действия:", "keyboard": keyboard})
            else:
                fields = {"message": text[:20000], "urlPreview": False}
                if keyboard:
                    fields["keyboard"] = keyboard
                await self.call("imbot.v2.Chat.Message.send", dialogId=dialog, fields=fields)
            return True
        except Exception as e:
            log.warning("Не удалось отправить сообщение: %s", e)
            return False

    async def broadcast(self, text: str, keyboard: list | None = MENU, photo: tuple[str, str] | None = None):
        if self.ready and self.dialog_id:
            await self.send(self.dialog_id, text, keyboard, photo)

    # --- события от объекта -------------------------------------------------------
    async def on_site_notify(self, msg: dict):
        keyboard = MENU
        if msg.get("buttons") is not None:
            keyboard = [btn(b["text"], b["command"], b.get("params", ""), b.get("color", "base"))
                        for b in msg["buttons"]] or None
        photo = None
        if msg.get("photo"):
            photo = (msg["photo"].get("name", "snapshot.jpg"), msg["photo"]["b64"])
        await self.broadcast(msg.get("text", ""), keyboard, photo)

    async def on_site_connect(self, down_since: float):
        now = time.time()
        down = now - down_since
        alerted, self.offline_alerted = self.offline_alerted, False
        if down < self.cfg.outage_min and not alerted:
            return
        lines = ["[B]🔌 Связь с объектом восстановлена[/B]",
                 f"Объект был не на связи {fmt_duration(down)}: с {fmt_time(down_since)} по {fmt_time(now)}."]
        try:
            summary = await self.link.request("outage_summary", timeout=self.cfg.command_timeout,
                                              since=down_since, until=now)
            if summary:
                lines.append(summary)
        except (SiteOffline, asyncio.TimeoutError):
            pass
        await self.broadcast("\n".join(lines))

    async def watch_site(self):
        """Сообщает в чат, если объект пропал со связи надолго."""
        while True:
            after = self.cfg.site_offline_alert_after
            if (after and not self.link.online and not self.offline_alerted
                    and time.time() - self.link.disconnected_at >= after and self.ready):
                self.offline_alerted = True
                await self.broadcast(
                    f"[B]🔴 Нет связи с объектом с {fmt_time(self.link.disconnected_at)}[/B]\n"
                    "Скорее всего, на объекте пропал интернет или выключен компьютер. Ворота при этом "
                    "продолжают открываться по номерам и с пульта. Сообщу, когда связь восстановится.")
            await asyncio.sleep(10)

    # --- приём событий Битрикс24 ------------------------------------------------------
    async def poll_loop(self):
        if not self.enabled:
            log.warning("Бот Битрикс24 отключён: не заданы B24_WEBHOOK_URL и B24_BOT_TOKEN")
            return
        while not self.ready:
            try:
                await self.setup()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.b24_error = str(e)
                log.exception("Не удалось настроить бота (проверьте B24_WEBHOOK_URL и права imbot)")
                await asyncio.sleep(30)
        while True:
            try:
                params = {"limit": 100}
                if self.state.get("offset"):
                    params["offset"] = self.state["offset"]
                res = await self.call("imbot.v2.Event.get", **params)
                self.b24_error = ""
                for ev in res.get("events", []):
                    try:
                        await self.handle(ev)
                    except Exception:
                        log.exception("Ошибка обработки события")
                if res.get("nextOffset"):
                    self.state["offset"] = res["nextOffset"]
                    self._save_state()
                if res.get("hasMore"):
                    continue
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, B24Error) as e:
                self.b24_error = str(e)
                log.warning("Битрикс24: %s", e)
                if isinstance(e, B24Error) and e.code == "QUERY_LIMIT_EXCEEDED":
                    await asyncio.sleep(5)
            await asyncio.sleep(self.cfg.poll_interval)

    async def handle(self, ev: dict):
        kind, data = ev.get("type"), ev.get("data", {})
        user = data.get("user", {})
        if user.get("bot"):
            return
        dialog = data.get("chat", {}).get("dialogId") or data.get("dialogId", "")
        uid = int(user.get("id") or 0)
        who = user.get("name") or user.get("firstName") or f"id {uid}"
        age = time.time() - event_ts(ev)

        if kind == "ONIMBOTV2JOINCHAT":
            await self.send(dialog, f"Здравствуйте! Я бот ворот.\n\n{HELP}")
            return
        if kind == "ONIMBOTV2COMMANDADD":
            c = data.get("command", {})
            cmd, params = c.get("command", "").lstrip("/").lower(), (c.get("params") or "").strip()
        elif kind == "ONIMBOTV2MESSAGEADD":
            text = (data.get("message", {}).get("text") or "").strip()
            if text.startswith("/"):
                return  # команды приходят отдельным событием ONIMBOTV2COMMANDADD
            cmd, params = "help", ""
        else:
            return

        if uid not in self.cfg.user_ids:
            log.warning("Команда от сотрудника без доступа: %s (id %s)", who, uid)
            await self.send(dialog, f"Нет доступа к управлению воротами. Ваш ID в Битрикс24: {uid}. "
                                    "Попросите администратора добавить его в B24_USER_IDS.", None)
            return
        if cmd not in FORWARDED:
            await self.send(dialog, HELP)
            return
        if cmd == "open" and age > self.cfg.max_command_age:
            await self.send(dialog, f"⏱ Команда отправлена {fmt_duration(age)} назад и не выполнена. "
                                    "Нажмите ещё раз, если ворота всё ещё нужно открыть.")
            return
        await self.send(dialog, await self.forward(cmd, params, who))

    async def forward(self, cmd: str, params: str, who: str) -> str:
        try:
            return await self.link.request(cmd, timeout=self.cfg.command_timeout, params=params, who=who)
        except SiteOffline:
            return self.offline_text(cmd)
        except asyncio.TimeoutError:
            return "⚠️ Объект не ответил вовремя. Попробуйте ещё раз или проверьте «📡 Статус»."

    def offline_text(self, cmd: str) -> str:
        since = self.link.disconnected_at
        head = (f"🔴 Нет связи с объектом с {fmt_time(since)} ({fmt_duration(time.time() - since)}). "
                "Скорее всего, на объекте пропал интернет или выключен компьютер.")
        if cmd == "open":
            return head + "\nКоманда не выполнена. Ворота можно открыть пультом."
        if cmd == "status":
            lines = [f"[B]📡 Состояние на {time.strftime('%d.%m.%Y %H:%M:%S')}[/B]", "🟢 Бот на VPS: работает", head]
            if self.link.last_status:
                ts, text = self.link.last_status
                lines += ["", f"Последние данные от объекта ({fmt_time(ts)}):", text]
            return "\n".join(lines)
        return head + "\nДанные хранятся на объекте и сейчас недоступны."
