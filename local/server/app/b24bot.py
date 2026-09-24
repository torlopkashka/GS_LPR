"""Чат-бот Битрикс24: уведомления, управление воротами, список номеров и контроль связи.

Используется API «Чат-боты 2.0» (imbot.v2) в режиме fetch: бот сам забирает
события с портала методом imbot.v2.Event.get. Поэтому ПК у ворот не нужен
белый IP или публичный адрес, достаточно исходящего доступа в интернет.
Авторизация — входящий вебхук Битрикс24 с правами imbot плюс секрет бота (botToken).

Бот работает на том же ПК, что и сервер. При пропаже интернета на объекте
он не может ответить вообще, поэтому:
  * команда «Статус»: если ответ пришёл, сервер работает и интернет есть;
  * после восстановления связи бот присылает отчёт об обрыве, а команды
    «открыть», нажатые во время обрыва, не выполняет (у каждого события
    Битрикс24 есть время нажатия);
  * о самом обрыве сообщает внешний сторож (Uptime Kuma), см. monitor_loop.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

import httpx

from .config import Config
from .plates import display, normalize

log = logging.getLogger("lpr.bitrix24")

TITLES = {
    "granted": "✅ Проезд разрешён",
    "denied": "⛔ Неизвестный номер",
    "logged": "📷 Номер распознан",
    "error": "⚠️ Не удалось открыть ворота",
    "manual": "🔓 Открыто вручную",
}
DECISION_SHORT = {
    "granted": "открыто", "denied": "отказ", "manual": "вручную", "logged": "распознан",
    "error": "ошибка", "system": "система",
}

# Команды бота: имя без «/» -> (заголовок, подсказка к параметрам)
COMMANDS = {
    "open": ("Открыть ворота", ""),
    "status": ("Состояние сервера, агента ворот и камер", ""),
    "last": ("Последние проезды", ""),
    "list": ("Разрешённые номера", ""),
    "add": ("Добавить номер", "А123ВС77 Владелец [до 31.12.2026]"),
    "del": ("Удалить номер", "А123ВС77"),
    "help": ("Справка", ""),
}

HELP = (
    "[B]Кнопки под сообщениями бота:[/B]\n"
    "🔓 Открыть ворота — подать команду на ворота\n"
    "📡 Статус — на связи ли сервер, агент ворот и камеры\n"
    "🕘 Последние — последние 10 событий\n"
    "📋 Номера — разрешённые номера\n\n"
    "[B]Команды:[/B]\n"
    "/add А123ВС77 Иванов — добавить номер (владелец необязателен)\n"
    "/add А123ВС77 Гость до 31.12.2026 — временный доступ\n"
    "/del А123ВС77 — удалить номер\n\n"
    "Если бот не отвечает на «Статус», на объекте нет интернета или выключен компьютер. "
    "Ворота при этом продолжают открываться по номерам и с пульта."
)


def _btn(text: str, command: str, params: str = "", color: str = "base") -> dict:
    b = {"TEXT": text, "COMMAND": command, "DISPLAY": "LINE", "BG_COLOR_TOKEN": color}
    if params:
        b["COMMAND_PARAMS"] = params
    return b


MENU = [
    _btn("🔓 Открыть ворота", "open", color="primary"),
    _btn("📡 Статус", "status"),
    _btn("🕘 Последние", "last"),
    _btn("📋 Номера", "list"),
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


def parse_date(token: str) -> str | None:
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", token)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
        return token
    return None


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
    def __init__(self, cfg: Config, db, hub, workers: dict, cameras: dict):
        self.cfg = cfg
        self.bx = cfg.bitrix24
        self.db = db
        self.hub = hub
        self.workers = workers
        self.cameras = cameras
        self.controller = None  # AccessController, задаётся в main
        self.started_at = time.time()
        self.client = httpx.AsyncClient(timeout=30)
        self.http = httpx.AsyncClient(timeout=10)  # для внешнего сторожа
        self.state_file = cfg.data_dir / "bitrix24.json"
        self.state = self._load_state()
        self.bot_id: int | None = None
        self.ready = False
        # состояние связи с интернетом (по доступности Битрикс24)
        self.last_ok = 0.0
        self.offline_since: float | None = None
        self.last_outage: tuple[float, float] | None = None
        self._alerted: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.bx.webhook_url and self.bx.bot_token)

    @property
    def dialog_id(self) -> str:
        return self.bx.dialog_id or self.state.get("dialog_id", "")

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
    async def _call(self, method: str, **params) -> dict:
        """Вызов REST. Сетевые ошибки пробрасываются как httpx.HTTPError, ошибки API — B24Error."""
        if method.startswith("imbot.v2.") and method != "imbot.v2.Bot.register":
            params.setdefault("botToken", self.bx.bot_token)
            if self.bot_id is not None and method not in ("imbot.v2.Bot.get", "imbot.v2.Bot.list"):
                params.setdefault("botId", self.bot_id)
        url = self.bx.webhook_url.rstrip("/") + "/" + method
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
            res = await self._call("imbot.v2.Bot.get", code=self.bx.bot_code)
            self.bot_id = int(res["bot"]["id"])
        except B24Error as e:
            if e.code not in ("BOT_NOT_FOUND",):
                raise
            res = await self._call("imbot.v2.Bot.register", fields={
                "code": self.bx.bot_code,
                "botToken": self.bx.bot_token,
                "type": "bot",
                "eventMode": "fetch",
                "properties": {"name": self.bx.bot_name, "workPosition": "Распознавание номеров, ворота"},
            })
            self.bot_id = int(res["bot"]["id"])
            log.info("Бот Битрикс24 зарегистрирован, id=%s", self.bot_id)

        existing = await self._call("imbot.v2.Command.list")
        have = {c["command"].lstrip("/") for c in existing.get("commands", [])}
        for cmd, (title, params) in COMMANDS.items():
            if cmd in have:
                continue
            fields = {"command": cmd, "title": {"ru": title, "en": title}, "common": False}
            if params:
                fields["params"] = {"ru": params, "en": params}
            await self._call("imbot.v2.Command.register", fields=fields)

        if not self.dialog_id and self.bx.user_ids:
            res = await self._call("imbot.v2.Chat.add", fields={
                "title": self.bx.bot_name, "userIds": self.bx.user_ids,
                "description": "Уведомления о проездах и управление воротами",
            })
            self.state["dialog_id"] = res["chat"]["dialogId"]
            self._save_state()
            log.info("Создан чат для уведомлений: %s", self.dialog_id)
            await self.send(self.dialog_id, "Чат создан. Сюда будут приходить уведомления о проездах.\n\n" + HELP)
        elif self.state.get("dialog_id") and not self.bx.dialog_id and self.bx.user_ids:
            # Чат создан ботом: добавить в него сотрудников, появившихся в B24_USER_IDS
            try:
                members = await self._call("imbot.v2.Chat.User.list", dialogId=self.dialog_id, limit=200)
                have_ids = {int(u["id"]) for u in (members if isinstance(members, list) else members.get("users", []))}
                missing = [u for u in self.bx.user_ids if u not in have_ids]
                if missing:
                    await self._call("imbot.v2.Chat.User.add", dialogId=self.dialog_id, userIds=missing)
                    log.info("В чат добавлены сотрудники: %s", missing)
            except B24Error as e:
                log.warning("Не удалось обновить участников чата: %s", e)
        self.ready = True
        log.info("Бот Битрикс24 готов (id=%s, чат %s)", self.bot_id, self.dialog_id or "не задан")

    # --- отправка ---------------------------------------------------------------------
    async def send(self, dialog: str, text: str, keyboard: list | None = MENU, photo: Path | None = None) -> bool:
        try:
            if photo and photo.exists():
                content = base64.b64encode(photo.read_bytes()).decode()
                res = await self._call("imbot.v2.File.upload", dialogId=dialog,
                                       fields={"name": photo.name, "content": content, "message": text})
                if keyboard:
                    # File.upload не принимает кнопки: добавляем их к сообщению с фото,
                    # а если не вышло — отдельным сообщением
                    try:
                        await self._call("imbot.v2.Chat.Message.update", messageId=res.get("messageId"),
                                         fields={"keyboard": keyboard})
                    except B24Error:
                        await self._call("imbot.v2.Chat.Message.send", dialogId=dialog,
                                         fields={"message": "Действия:", "keyboard": keyboard})
            else:
                fields = {"message": text[:20000], "urlPreview": False}
                if keyboard:
                    fields["keyboard"] = keyboard
                await self._call("imbot.v2.Chat.Message.send", dialogId=dialog, fields=fields)
            return True
        except Exception as e:
            log.warning("Битрикс24: не удалось отправить сообщение: %s", e)
            return False

    async def broadcast(self, text: str, keyboard: list | None = MENU, photo: Path | None = None):
        if not self.enabled or not self.ready or self.offline_since is not None or not self.dialog_id:
            return  # без интернета не ждём таймаутов; о пропущенном расскажет отчёт о восстановлении
        await self.send(self.dialog_id, text, keyboard, photo)

    # --- уведомления о проездах ------------------------------------------------------
    async def event(self, ev: dict | None, snapshot: Path | None):
        if not self.enabled or ev is None:
            return
        d = ev["decision"]
        if (d == "granted" and not self.bx.notify_granted) or (d == "denied" and not self.bx.notify_denied):
            return
        cam = self.cameras.get(ev["camera"])
        lines = [f"[B]{TITLES.get(d, d)}: {display(ev['plate'])}[/B]"]
        if ev.get("owner"):
            lines.append(f"Владелец: {ev['owner']}")
        lines.append(f"Камера: {cam.name if cam else ev['camera']}")
        if ev.get("detail") and d != "denied":
            lines.append(ev["detail"])
        lines.append(time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(ev["ts"])))
        keyboard = None
        if d in ("denied", "logged", "error") and self.bx.interactive:
            keyboard = [_btn("🔓 Открыть ворота", "open", f"ev:{ev['id']}", "primary")]
            if ev["plate"]:
                keyboard.append(_btn("➕ В список", "add", f"ev:{ev['id']}"))
        await self.broadcast("\n".join(lines), keyboard, snapshot)

    # --- тексты ----------------------------------------------------------------------------
    def status_text(self) -> str:
        now = time.time()
        lines = [f"[B]📡 Состояние на {time.strftime('%d.%m.%Y %H:%M:%S')}[/B]",
                 f"🟢 Сервер: работает {fmt_duration(now - self.started_at)}"]
        if self.hub.online:
            info = self.hub.info
            extra = ", ".join(x for x in (info.get("driver"), info.get("name")) if x)
            lines.append("🟢 Агент ворот: на связи" + (f" ({extra})" if extra else ""))
        else:
            lines.append(f"🔴 Агент ворот: НЕ на связи {fmt_duration(now - self.hub.disconnected_at)}"
                         " — ворота не откроются автоматически")
        for w in self.workers.values():
            st = w.status()
            if st["connected"]:
                lines.append(f"🟢 {st['name']}: видео есть")
            else:
                why = f" ({st['error']})" if st["error"] else ""
                lines.append(f"🔴 {st['name']}: нет видео{why}")
        for cam in self.cameras.values():
            if not cam.enabled:
                lines.append(f"⚪ {cam.name}: отключена в настройках")
        if self.last_outage:
            a, b = self.last_outage
            lines.append(f"🌐 Последний обрыв интернета: {fmt_time(a)}–{fmt_time(b)} ({fmt_duration(b - a)})")
        else:
            lines.append("🌐 Обрывов интернета с момента запуска не было")
        last = [e for e in self.db.list_events(limit=5) if e["plate"]]
        if last:
            e = last[0]
            lines.append(f"🚗 Последний номер: {display(e['plate'])} в {fmt_time(e['ts'])}"
                         f" ({DECISION_SHORT.get(e['decision'], e['decision'])})")
        return "\n".join(lines)

    def last_events_text(self, n: int = 10) -> str:
        events = self.db.list_events(limit=n)
        if not events:
            return "Событий пока нет"
        out = ["[B]🕘 Последние события:[/B]"]
        for e in events:
            cam = self.cameras.get(e["camera"])
            who = f" — {e['owner']}" if e["owner"] else ""
            plate = display(e["plate"]) if e["plate"] else (e["detail"] or "—")
            out.append(f"{fmt_time(e['ts'])} {plate}{who} · {DECISION_SHORT.get(e['decision'], e['decision'])}"
                       + (f" · {cam.name}" if cam else ""))
        return "\n".join(out)

    def plates_text(self) -> str:
        plates = self.db.list_plates()
        if not plates:
            return "Список пуст. Добавить: /add А123ВС77 Владелец"
        today = time.strftime("%Y-%m-%d")
        out = [f"[B]📋 Разрешённые номера ({len(plates)}):[/B]"]
        for p in plates:
            mark = ""
            if not p["active"]:
                mark = " (отключён)"
            elif p["valid_until"]:
                mark = " (истёк)" if p["valid_until"] < today else f" (до {p['valid_until']})"
            owner = f" — {p['owner']}" if p["owner"] else ""
            out.append(f"{display(p['plate'])}{owner}{mark}")
        return "\n".join(out)

    # --- приём событий --------------------------------------------------------------
    async def poll_loop(self):
        if not self.enabled:
            return
        while not self.ready:
            try:
                await self.setup()
                await self._mark_online()
            except asyncio.CancelledError:
                raise
            except httpx.HTTPError as e:
                self._mark_offline(e)
                await asyncio.sleep(10)
            except Exception:
                log.exception("Не удалось настроить бота Битрикс24 (проверьте B24_WEBHOOK_URL и права imbot)")
                await asyncio.sleep(60)
        while True:
            try:
                params = {"limit": 100}
                if self.state.get("offset"):
                    params["offset"] = self.state["offset"]
                res = await self._call("imbot.v2.Event.get", **params)
                await self._mark_online()
                for ev in res.get("events", []):
                    try:
                        await self._handle(ev)
                    except Exception:
                        log.exception("Ошибка обработки события Битрикс24")
                if res.get("nextOffset"):
                    self.state["offset"] = res["nextOffset"]
                    self._save_state()
                if res.get("hasMore"):
                    continue
            except asyncio.CancelledError:
                raise
            except httpx.HTTPError as e:
                self._mark_offline(e)
            except B24Error as e:
                log.warning("Битрикс24: %s", e)
                if e.code == "QUERY_LIMIT_EXCEEDED":
                    await asyncio.sleep(5)
            await asyncio.sleep(self.bx.poll_interval)

    def _mark_offline(self, err: Exception):
        if self.offline_since is None:
            self.offline_since = self.last_ok or time.time()
            log.warning("Нет связи с Битрикс24: %s", err)

    async def _mark_online(self):
        now = time.time()
        since, self.offline_since = self.offline_since, None
        self.last_ok = now
        if since is None or now - since < self.bx.outage_min:
            return
        self.last_outage = (since, now)
        log.warning("Связь восстановлена, не было %s", fmt_duration(now - since))
        counts = self.db.count_events(since, now)
        self.db.add_event(camera="-", decision="system", detail=f"нет интернета {fmt_duration(now - since)}")
        lines = [
            "[B]🔌 Связь с объектом восстановлена[/B]",
            f"Интернета не было {fmt_duration(now - since)}: с {fmt_time(since)} по {fmt_time(now)}.",
            "Ворота всё это время работали: распознавание и агент не зависят от интернета.",
        ]
        parts = []
        for key, name in (("granted", "открыто по номеру"), ("denied", "отказов"),
                          ("manual", "открыто вручную"), ("error", "ошибок открытия")):
            if counts.get(key):
                parts.append(f"{name}: {counts[key]}")
        lines.append("За это время " + (", ".join(parts) if parts else "проездов не было") + ".")
        lines.append("Команды «открыть», отправленные во время обрыва, не выполнялись.")
        await self.broadcast("\n".join(lines))

    async def _handle(self, ev: dict):
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

        if uid not in self.bx.user_ids:
            log.warning("Команда от сотрудника без доступа: %s (id %s)", who, uid)
            await self.send(dialog, f"Нет доступа к управлению воротами. Ваш ID в Битрикс24: {uid}. "
                                    "Попросите администратора добавить его в B24_USER_IDS.", None)
            return

        if cmd == "open":
            await self.send(dialog, await self._cmd_open(params, who, age))
        elif cmd == "status":
            await self.send(dialog, self.status_text())
        elif cmd == "last":
            await self.send(dialog, self.last_events_text())
        elif cmd == "list":
            await self.send(dialog, self.plates_text())
        elif cmd == "add":
            await self.send(dialog, self._cmd_add(params, who))
        elif cmd == "del":
            await self.send(dialog, self._cmd_del(params))
        else:
            await self.send(dialog, HELP)

    async def _cmd_open(self, params: str, who: str, age: float) -> str:
        if age > self.bx.max_command_age:
            return (f"⏱ Команда отправлена {fmt_duration(age)} назад, когда на объекте не было интернета, "
                    "и не выполнена. Нажмите ещё раз, если ворота всё ещё нужно открыть.")
        ev = None
        if params.startswith("ev:") and params[3:].isdigit():
            ev = self.db.get_event(int(params[3:]))
            if ev and time.time() - ev["ts"] > self.bx.max_callback_age:
                return "Уведомление устарело. Нажмите «🔓 Открыть ворота» в меню."
        ok, detail = await self.controller.open_gate(f"Битрикс24 ({who})", force=True)
        if ev:
            if ok:
                self.db.update_event(ev["id"], decision="manual", detail=f"открыто из Битрикс24: {who}")
        else:
            self.db.add_event(camera="-", decision="manual" if ok else "error", detail=f"Битрикс24, {who}: {detail}")
        return f"🔓 Ворота открываются — {who}" if ok else f"⚠️ Не удалось открыть: {detail}"

    def _cmd_add(self, args: str, who: str) -> str:
        if args.startswith("ev:") and args[3:].isdigit():
            ev = self.db.get_event(int(args[3:]))
            if not ev or not ev["plate"]:
                return "Событие не найдено"
            self.db.add_plate(ev["plate"], "", f"добавлен из Битрикс24 ({who})")
            compact = display(ev["plate"]).replace(" ", "")
            return f"➕ {display(ev['plate'])} добавлен в список — {who}\nВладельца можно указать: /add {compact} Имя"
        tokens = args.split()
        if not tokens:
            return "Формат: /add А123ВС77 Владелец [до 31.12.2026]"
        plate = normalize(tokens[0])
        if len(plate) < 4:
            return "Некорректный номер. Пишите слитно: /add А123ВС77"
        rest, valid = tokens[1:], None
        if rest and parse_date(rest[-1]):
            valid = parse_date(rest[-1])
            rest = rest[:-1]
            if rest and rest[-1].lower() == "до":
                rest = rest[:-1]
        owner = " ".join(rest)
        self.db.add_plate(plate, owner, f"добавлен из Битрикс24 ({who})", valid)
        return f"✅ {display(plate)} добавлен" + (f" ({owner})" if owner else "") + (f", до {valid}" if valid else "")

    def _cmd_del(self, args: str) -> str:
        plate = normalize(args)
        row = self.db.find_plate(plate) if plate else None
        if not row:
            return "Номер не найден. Формат: /del А123ВС77"
        self.db.delete_plate(row["id"])
        return f"🗑 {display(plate)} удалён"

    # --- контроль агента, камер и внешний сторож -----------------------------------
    async def monitor_loop(self):
        last_ping = 0.0
        while True:
            try:
                await self._check_alerts()
                if self.cfg.healthcheck.url and time.time() - last_ping >= self.cfg.healthcheck.interval:
                    last_ping = time.time()
                    await self._ping_healthcheck()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ошибка мониторинга")
            await asyncio.sleep(5)

    def problems(self) -> list[str]:
        out = []
        if not self.hub.online:
            out.append("агент ворот не на связи")
        for w in self.workers.values():
            if not w.status()["connected"]:
                out.append(f"нет видео: {w.cam.name}")
        return out

    async def _alert(self, key: str, down: bool, since: float, after: float, down_text: str, up_text: str):
        now = time.time()
        if down and key not in self._alerted and now - since >= after:
            self._alerted[key] = since
            await self.broadcast(f"⚠️ {down_text} (с {fmt_time(since)})")
        elif not down and key in self._alerted:
            since = self._alerted.pop(key)
            await self.broadcast(f"✅ {up_text} (не было {fmt_duration(now - since)})")

    async def _check_alerts(self):
        if not self.enabled or not self.ready or self.offline_since is not None:
            return  # без связи предупреждения всё равно не уйдут — проверим после восстановления
        await self._alert("agent", not self.hub.online, self.hub.disconnected_at, self.bx.agent_alert_after,
                          "Агент ворот не на связи — автоматическое открытие не работает. "
                          "Проверьте, запущен ли агент на ПК и подключено ли реле.",
                          "Агент ворот снова на связи")
        for w in self.workers.values():
            st = w.status()
            down_since = w.reader.frame_ts or self.started_at
            await self._alert(f"cam:{w.cam.id}", not st["connected"], down_since, self.bx.camera_alert_after,
                              f"Нет видео с камеры «{w.cam.name}»", f"Камера «{w.cam.name}» снова работает")

    async def _ping_healthcheck(self):
        """Сигнал внешнему сторожу. Поддерживаются два формата:
        Uptime Kuma (адрес содержит /api/push/) и healthchecks.io (и совместимые).
        """
        problems = self.problems()
        msg = "; ".join(problems) or "OK"
        base = self.cfg.healthcheck.url.strip()
        try:
            if "/api/push/" in base:
                url = base.split("?")[0]
                await self.http.get(url, params={"status": "down" if problems else "up", "msg": msg})
            else:
                url = base.rstrip("/") + ("/fail" if problems else "")
                await self.http.post(url, content=msg)
        except Exception as e:
            log.debug("Сторож недоступен: %s", e)
