"""Чат-бот Битрикс24 на VPS: уведомления о проездах и ведение списка номеров.

API «Чат-боты 2.0» (imbot.v2) в режиме fetch: бот сам забирает события
методом imbot.v2.Event.get. Авторизация — входящий вебхук с правами imbot
плюс секрет бота (botToken).

Воротами бот НЕ управляет: открыть ворота из Битрикс24 нельзя. Основной
список номеров хранится на ПК у ворот. Команды /add и /del ставятся в
очередь, ПК забирает их при очередном обмене (раз в ~10 с), применяет у себя
и присылает результат, который бот пересылает сотруднику.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

from .config import BotConfig
from .site import SiteState

log = logging.getLogger("vps.bitrix24")

# Команды бота: имя без «/» -> (заголовок, подсказка к параметрам)
COMMANDS = {
    "status": ("Состояние объекта: сервер, агент ворот, камеры", ""),
    "last": ("Последние проезды", ""),
    "list": ("Разрешённые номера", ""),
    "add": ("Добавить номер", "А123ВС77 Владелец [до 31.12.2026]"),
    "del": ("Удалить номер", "А123ВС77"),
    "help": ("Справка", ""),
}

HELP = (
    "[B]Кнопки под сообщениями бота:[/B]\n"
    "📡 Статус — на связи ли объект, агент ворот и камеры\n"
    "🕘 Последние — последние 10 событий\n"
    "📋 Номера — разрешённые номера\n\n"
    "[B]Команды:[/B]\n"
    "/add А123ВС77 Иванов — добавить номер (владелец необязателен)\n"
    "/add А123ВС77 Гость до 31.12.2026 — временный доступ\n"
    "/del А123ВС77 — удалить номер\n\n"
    "Ворота открываются автоматически по номерам из списка или пультом. "
    "Из Битрикс24 ворота не открываются."
)


def btn(text: str, command: str, params: str = "", color: str = "base") -> dict:
    b = {"TEXT": text, "COMMAND": command, "DISPLAY": "LINE", "BG_COLOR_TOKEN": color}
    if params:
        b["COMMAND_PARAMS"] = params
    return b


MENU = [
    btn("📡 Статус", "status", color="primary"),
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


class B24Error(Exception):
    def __init__(self, code: str, description: str = ""):
        super().__init__(f"{code}: {description}")
        self.code = code


class Bitrix24Bot:
    def __init__(self, cfg: BotConfig, site: SiteState):
        self.cfg = cfg
        self.site = site
        self.client = httpx.AsyncClient(timeout=30)
        self.state_file = cfg.data_dir / "bitrix24.json"
        self.state = self._load_state()
        self.bot_id: int | None = None
        self.ready = False
        self.b24_error = ""
        self.offline_alerted = False

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
        for c in existing.get("commands", []):
            if c["command"].lstrip("/") not in COMMANDS:  # например, старая команда /open
                try:
                    await self.call("imbot.v2.Command.unregister", commandId=c["id"])
                except B24Error as e:
                    log.warning("Не удалось удалить команду %s: %s", c["command"], e)
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
                        for b in msg["buttons"] if b.get("command") in ("add", "status", "last", "list")] or None
        photo = None
        if msg.get("photo"):
            photo = (msg["photo"].get("name", "snapshot.jpg"), msg["photo"]["b64"])
        await self.broadcast(msg.get("text", ""), keyboard, photo)

    async def on_site_sync(self, payload: dict) -> list[dict]:
        """Обмен с объектом: принять данные, отдать правки, переслать результаты сотрудникам."""
        was_offline_since = None if self.site.online else self.site.last_sync
        ops, done = self.site.sync(payload)
        for op, text in done:
            await self.send(op["dialog"], text)
        if self.offline_alerted:
            self.offline_alerted = False
            since = was_offline_since or self.site.last_sync
            await self.broadcast(f"[B]🟢 Связь с объектом восстановлена[/B] (не было {fmt_duration(time.time() - since)})")
        return ops

    async def watch_site(self):
        """Сообщает в чат, если объект перестал выходить на связь."""
        while True:
            after = self.cfg.site_offline_alert_after
            last = self.site.last_sync
            if (after and last and not self.offline_alerted and self.ready
                    and time.time() - last >= after):
                self.offline_alerted = True
                await self.broadcast(
                    f"[B]🔴 Нет связи с объектом с {fmt_time(last)}[/B]\n"
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
                        log.exception("Ошибка обработки события: %s", json.dumps(ev, ensure_ascii=False)[:2000])
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

    def parse_event(self, ev: dict) -> dict | None:
        """Разбирает событие imbot.v2 в {kind, cmd, params, uid, who, is_bot, dialog}.

        Портал Битрикс24 кодирует пустые объекты как пустой список «[]», а часть полей
        в реальных событиях может отсутствовать, поэтому каждое поле читается осторожно.
        """
        def obj(x) -> dict:
            return x if isinstance(x, dict) else {}

        kind = ev.get("type")
        data = obj(ev.get("data"))
        user, chat, msg = obj(data.get("user")), obj(data.get("chat")), obj(data.get("message"))
        uid = int(user.get("id") or msg.get("authorId") or 0)
        dialog = str(chat.get("dialogId") or data.get("dialogId") or "")
        if not dialog:
            chat_id = chat.get("id") or msg.get("chatId")
            chat_type = chat.get("type")
            if chat_id and self.dialog_id == f"chat{chat_id}":
                dialog = self.dialog_id                 # наш групповой чат «Ворота»
            elif chat_id and chat_type and chat_type != "private":
                dialog = f"chat{chat_id}"
            elif uid:
                dialog = str(uid)                       # личный диалог: dialogId = ID сотрудника
        if kind == "ONIMBOTV2COMMANDADD":
            c = obj(data.get("command"))
            cmd = str(c.get("command") or "").lstrip("/").lower()
            params = str(c.get("params") or "").strip()
            if not cmd:  # формат команды неизвестен — берём из текста сообщения
                text = str(msg.get("text") or "").strip()
                cmd, _, params = text.lstrip("/").partition(" ")
                cmd, params = cmd.lower(), params.strip()
        elif kind == "ONIMBOTV2MESSAGEADD":
            text = str(msg.get("text") or "").strip()
            if text.startswith("/"):
                return None  # команды приходят отдельным событием ONIMBOTV2COMMANDADD
            cmd, params = "help", ""
        elif kind == "ONIMBOTV2JOINCHAT":
            cmd, params = "join", ""
        else:
            return None
        return {"kind": kind, "cmd": cmd, "params": params, "uid": uid, "dialog": dialog,
                "who": user.get("name") or user.get("firstName") or f"id {uid}",
                "is_bot": bool(user.get("bot"))}

    async def handle(self, ev: dict):
        p = self.parse_event(ev)
        if p is None or p["is_bot"]:
            return
        if not p["dialog"]:
            log.warning("Не удалось определить диалог для ответа, событие: %s",
                        json.dumps(ev, ensure_ascii=False)[:2000])
            return
        log.info("Команда /%s от %s (id %s) в %s", p["cmd"], p["who"], p["uid"], p["dialog"])
        cmd, params, uid, who, dialog = p["cmd"], p["params"], p["uid"], p["who"], p["dialog"]
        if cmd == "join":
            await self.send(dialog, f"Здравствуйте! Я бот ворот.\n\n{HELP}")
            return

        if uid not in self.cfg.user_ids:
            log.warning("Команда от сотрудника без доступа: %s (id %s)", who, uid)
            await self.send(dialog, f"Нет доступа к боту ворот. Ваш ID в Битрикс24: {uid}. "
                                    "Попросите администратора добавить его в B24_USER_IDS.", None)
            return
        await self.send(dialog, self.answer(cmd, params, who, dialog))

    def offline_note(self) -> str:
        last = self.site.last_sync
        if not last:
            return "🔴 Объект ещё ни разу не выходил на связь с ботом."
        return (f"🔴 Нет связи с объектом с {fmt_time(last)} ({fmt_duration(time.time() - last)}). "
                "Скорее всего, на объекте пропал интернет или выключен компьютер.")

    def answer(self, cmd: str, params: str, who: str, dialog: str) -> str:
        d = self.site.data
        synced = fmt_time(d["last_sync"]) if d["last_sync"] else "—"
        if cmd in ("add", "del"):
            if not params:
                return "Формат: /add А123ВС77 Владелец [до 31.12.2026]" if cmd == "add" else "Формат: /del А123ВС77"
            self.site.add_op(cmd, params, who, dialog)
            if self.site.online:
                return "⏳ Передаю на объект, ответ придёт в течение 15 секунд."
            return self.offline_note() + "\nКоманда сохранена и будет выполнена, когда объект выйдет на связь."
        if cmd == "status":
            head = f"[B]📡 Состояние на {time.strftime('%d.%m.%Y %H:%M:%S')}[/B]"
            if self.site.online:
                return f"{head}\n🟢 Объект на связи\n{d['status']}"
            text = f"{head}\n{self.offline_note()}"
            if d["status"]:
                text += f"\n\nПоследние данные от объекта ({synced}):\n{d['status']}"
            return text
        if cmd == "last":
            note = "" if self.site.online else self.offline_note() + "\n\n"
            return f"{note}[B]🕘 Последние события[/B] (данные объекта на {synced}):\n{d['last_events'] or '—'}"
        if cmd == "list":
            return self.plates_text()
        return HELP

    def plates_text(self) -> str:
        d = self.site.data
        today = time.strftime("%Y-%m-%d")
        lines = []
        if not self.site.online:
            lines += [self.offline_note(), ""]
        synced = fmt_time(d["last_sync"]) if d["last_sync"] else "—"
        lines.append(f"[B]📋 Разрешённые номера ({len(d['plates'])})[/B], данные объекта на {synced}:")
        for p in d["plates"]:
            mark = ""
            if not p.get("active", True):
                mark = " (отключён)"
            elif p.get("valid_until"):
                mark = " (истёк)" if p["valid_until"] < today else f" (до {p['valid_until']})"
            owner = f" — {p['owner']}" if p.get("owner") else ""
            lines.append(f"{p['plate']}{owner}{mark}")
        if not d["plates"]:
            lines.append("Список пуст. Добавить: /add А123ВС77 Владелец")
        if self.site.ops:
            lines += ["", "[B]Ожидают применения на объекте:[/B]"]
            lines += [f"/{op['cmd']} {op['args']} — {op['who']}" for op in self.site.ops]
        return "\n".join(lines)
