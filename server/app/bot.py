"""Telegram-бот: уведомления, управление воротами, список номеров и контроль связи.

Бот работает на том же ПК, что и сервер, поэтому при пропаже интернета на
объекте он не может ответить вообще. Отсюда три механизма:
  * кнопка «📡 Статус» — если ответ пришёл, сервер работает и интернет есть,
    а в ответе видно состояние агента и камер;
  * после восстановления связи бот сам присылает отчёт: сколько не было
    интернета и что происходило у ворот за это время. Команды «открыть»,
    накопившиеся за время обрыва, не выполняются;
  * внешний «сторож» (Uptime Kuma / healthchecks.io): если сервер перестал выходить на связь,
    сторож сам пришлёт уведомление в Telegram (см. HealthcheckConfig).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

import httpx

from .config import Config
from .plates import display, normalize

log = logging.getLogger("lpr.telegram")

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

BTN_OPEN = "🔓 Открыть ворота"
BTN_STATUS = "📡 Статус"
BTN_LAST = "🕘 Последние проезды"
BTN_LIST = "📋 Номера"
MENU = json.dumps({
    "keyboard": [[{"text": BTN_OPEN}], [{"text": BTN_STATUS}, {"text": BTN_LAST}], [{"text": BTN_LIST}]],
    "resize_keyboard": True,
    "is_persistent": True,
})
HELP = (
    "Кнопки внизу экрана:\n"
    f"{BTN_OPEN} — подать команду на ворота\n"
    f"{BTN_STATUS} — проверить, на связи ли сервер, агент ворот и камеры\n"
    f"{BTN_LAST} — последние 10 событий\n"
    f"{BTN_LIST} — разрешённые номера\n\n"
    "Команды:\n"
    "/add А123ВС77 Иванов — добавить номер (владелец необязателен)\n"
    "/add А123ВС77 Гость до 31.12.2026 — временный доступ\n"
    "/del А123ВС77 — удалить номер\n\n"
    "Если бот не отвечает на «Статус», на объекте нет интернета или выключен компьютер. "
    "Ворота при этом продолжают открываться по номерам и с пульта."
)


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


class TelegramBot:
    def __init__(self, cfg: Config, db, hub, workers: dict, cameras: dict):
        self.cfg = cfg
        self.tg = cfg.telegram
        self.db = db
        self.hub = hub
        self.workers = workers
        self.cameras = cameras
        self.controller = None  # AccessController, задаётся в main
        self.started_at = time.time()
        self.api = f"{self.tg.api_url.rstrip('/')}/bot{self.tg.token}"
        # Запросы к Telegram — при необходимости через прокси; к сторожу — всегда напрямую
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(20, read=45), proxy=self.tg.proxy or None)
        self.http = httpx.AsyncClient(timeout=10)
        # состояние связи с интернетом (по доступности Telegram)
        self.last_ok = 0.0
        self.offline_since: float | None = None
        self.last_outage: tuple[float, float] | None = None
        self._stale_batch = False
        # предупреждения о потере агента/камер: ключ -> время отправки
        self._alerted: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.tg.token and self.tg.chat_ids)

    # --- низкий уровень ---------------------------------------------------------
    async def _call(self, method: str, **kw) -> dict:
        r = await self.client.post(f"{self.api}/{method}", **kw)
        data = r.json()
        if not data.get("ok"):
            log.warning("Telegram %s: %s", method, data.get("description"))
        return data

    async def _send_to(self, chat: int, text: str, markup: str | None = None, photo: Path | None = None) -> bool:
        data = {"chat_id": chat}
        if markup:
            data["reply_markup"] = markup
        try:
            if photo and photo.exists():
                data["caption"] = text[:1024]
                with open(photo, "rb") as f:
                    res = await self._call("sendPhoto", data=data, files={"photo": f})
            else:
                data["text"] = text[:4096]
                res = await self._call("sendMessage", data=data)
            return bool(res.get("ok"))
        except Exception as e:
            log.warning("Telegram: не удалось отправить сообщение: %s", e)
            return False

    async def broadcast(self, text: str, markup: str | None = None, photo: Path | None = None):
        if not self.enabled or self.offline_since is not None:
            return  # без интернета не ждём таймаутов; о пропущенном расскажет отчёт о восстановлении
        for chat in self.tg.chat_ids:
            await self._send_to(chat, text, markup, photo)

    # --- уведомления о проездах ---------------------------------------------------
    async def event(self, ev: dict | None, snapshot: Path | None):
        if not self.enabled or ev is None:
            return
        d = ev["decision"]
        if (d == "granted" and not self.tg.notify_granted) or (d == "denied" and not self.tg.notify_denied):
            return
        cam = self.cameras.get(ev["camera"])
        lines = [f"{TITLES.get(d, d)}: {display(ev['plate'])}"]
        if ev.get("owner"):
            lines.append(f"Владелец: {ev['owner']}")
        lines.append(f"Камера: {cam.name if cam else ev['camera']}")
        if ev.get("detail") and d != "denied":
            lines.append(ev["detail"])
        lines.append(time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(ev["ts"])))
        markup = None
        if d in ("denied", "logged", "error") and self.tg.interactive:
            buttons = [{"text": "🔓 Открыть ворота", "callback_data": f"open:{ev['id']}"}]
            if ev["plate"]:
                buttons.append({"text": "➕ В список", "callback_data": f"add:{ev['id']}"})
            markup = json.dumps({"inline_keyboard": [buttons]})
        await self.broadcast("\n".join(lines), markup, snapshot)

    # --- состояние ---------------------------------------------------------------------
    def status_text(self) -> str:
        now = time.time()
        lines = [f"📡 Состояние на {time.strftime('%d.%m.%Y %H:%M:%S')}",
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
        out = ["🕘 Последние события:"]
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
        out = [f"📋 Разрешённые номера ({len(plates)}):"]
        for p in plates:
            mark = ""
            if not p["active"]:
                mark = " (отключён)"
            elif p["valid_until"]:
                mark = " (истёк)" if p["valid_until"] < today else f" (до {p['valid_until']})"
            owner = f" — {p['owner']}" if p["owner"] else ""
            out.append(f"{display(p['plate'])}{owner}{mark}")
        return "\n".join(out)

    # --- приём команд -------------------------------------------------------------
    async def poll_loop(self):
        if not self.enabled:
            return
        try:
            await self._call("setMyCommands", data={"commands": json.dumps([
                {"command": "status", "description": "Состояние сервера, агента и камер"},
                {"command": "open", "description": "Открыть ворота"},
                {"command": "last", "description": "Последние события"},
                {"command": "list", "description": "Разрешённые номера"},
                {"command": "add", "description": "Добавить номер: /add А123ВС77 Владелец"},
                {"command": "del", "description": "Удалить номер: /del А123ВС77"},
                {"command": "help", "description": "Справка"},
            ])})
        except Exception:
            pass
        offset = 0
        while True:
            try:
                data = await self._call("getUpdates", data={"offset": offset, "timeout": 30})
                await self._mark_online()
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    try:
                        await self._handle(upd)
                    except Exception:
                        log.exception("Ошибка обработки сообщения Telegram")
                self._stale_batch = False
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._mark_offline(e)
                await asyncio.sleep(5)

    def _mark_offline(self, err: Exception):
        if self.offline_since is None:
            self.offline_since = self.last_ok or time.time()
            log.warning("Нет связи с Telegram: %s", err)

    async def _mark_online(self):
        now = time.time()
        since, self.offline_since = self.offline_since, None
        self.last_ok = now
        if since is None or now - since < self.tg.outage_min:
            return
        # Обновления, пришедшие первой пачкой после обрыва, копились в Telegram,
        # пока не было интернета. Опасные команды из них не выполняем.
        self._stale_batch = True
        self.last_outage = (since, now)
        log.warning("Связь восстановлена, не было %s", fmt_duration(now - since))
        counts = self.db.count_events(since, now)
        self.db.add_event(camera="-", decision="system",
                          detail=f"нет интернета {fmt_duration(now - since)}")
        lines = [
            "🔌 Связь с объектом восстановлена",
            f"Интернета не было {fmt_duration(now - since)}: с {fmt_time(since)} по {fmt_time(now)}.",
            "Ворота всё это время работали: распознавание и агент не зависят от интернета.",
        ]
        parts = []
        for key, name in (("granted", "открыто по номеру"), ("denied", "отказов"),
                          ("manual", "открыто вручную"), ("error", "ошибок открытия")):
            if counts.get(key):
                parts.append(f"{name}: {counts[key]}")
        lines.append("За это время " + (", ".join(parts) if parts else "проездов не было") + ".")
        lines.append("Команды, отправленные боту во время обрыва, не выполнялись.")
        await self.broadcast("\n".join(lines), MENU)

    def _allowed(self, chat_id) -> bool:
        return chat_id in self.tg.chat_ids

    async def _reply(self, chat: int, text: str, markup: str | None = MENU):
        await self._send_to(chat, text, markup)

    async def _open(self, who: str) -> str:
        ok, detail = await self.controller.open_gate(f"Telegram ({who})", force=True)
        self.db.add_event(camera="-", decision="manual" if ok else "error", detail=f"Telegram, {who}: {detail}")
        return "🔓 Ворота открываются" if ok else f"⚠️ Не удалось открыть: {detail}"

    async def _handle(self, upd: dict):
        if "callback_query" in upd:
            await self._handle_callback(upd["callback_query"])
            return
        msg = upd.get("message")
        if not msg:
            return
        chat = msg.get("chat", {}).get("id")
        if not self._allowed(chat):
            log.warning("Сообщение из неразрешённого чата %s", chat)
            await self._send_to(chat, f"Доступ запрещён. Ваш chat id: {chat}", None)
            return
        who = msg.get("from", {}).get("first_name", "") or msg.get("from", {}).get("username", "")
        text = (msg.get("text") or "").strip()
        cmd, _, args = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        age = time.time() - msg.get("date", time.time())

        if cmd in ("/open",) or text == BTN_OPEN:
            if self._stale_batch or age > self.tg.max_command_age:
                when = f" {fmt_duration(age)} назад" if age > self.tg.max_command_age else ""
                await self._reply(chat, f"⏱ Команда отправлена{when}, когда на объекте не было интернета, "
                                        "и не выполнена. Нажмите ещё раз, если ворота всё ещё нужно открыть.")
                return
            await self._reply(chat, await self._open(who))
        elif cmd == "/status" or text == BTN_STATUS:
            await self._reply(chat, self.status_text(), json.dumps(
                {"inline_keyboard": [[{"text": "🔄 Обновить", "callback_data": "status"}]]}))
        elif cmd == "/last" or text == BTN_LAST:
            await self._reply(chat, self.last_events_text())
        elif cmd == "/list" or text == BTN_LIST:
            await self._reply(chat, self.plates_text())
        elif cmd == "/add":
            await self._reply(chat, self._cmd_add(args, who))
        elif cmd == "/del":
            await self._reply(chat, self._cmd_del(args))
        else:
            await self._reply(chat, HELP)

    def _cmd_add(self, args: str, who: str) -> str:
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
        self.db.add_plate(plate, owner, f"добавлен из Telegram ({who})", valid)
        return f"✅ {display(plate)} добавлен" + (f" ({owner})" if owner else "") + (f", до {valid}" if valid else "")

    def _cmd_del(self, args: str) -> str:
        plate = normalize(args)
        row = self.db.find_plate(plate) if plate else None
        if not row:
            return "Номер не найден. Формат: /del А123ВС77"
        self.db.delete_plate(row["id"])
        return f"🗑 {display(plate)} удалён"

    async def _handle_callback(self, q: dict):
        message = q.get("message", {})
        chat = message.get("chat", {}).get("id")
        if not self._allowed(chat):
            return
        who = q["from"].get("first_name", "") or q["from"].get("username", "")
        action, _, ev_id = q.get("data", "").partition(":")
        answer = ""
        if action == "status":
            answer = "Обновлено"
            try:
                await self._call("editMessageText", data={
                    "chat_id": chat, "message_id": message["message_id"], "text": self.status_text(),
                    "reply_markup": json.dumps({"inline_keyboard": [[{"text": "🔄 Обновить", "callback_data": "status"}]]}),
                })
            except Exception:
                pass
        elif action == "open":
            age = time.time() - message.get("date", time.time())
            if self._stale_batch:
                answer = "Нажатие пришло после обрыва связи и не выполнено. Нажмите ещё раз."
            elif age > self.tg.max_callback_age:
                answer = f"Уведомление устарело. Используйте кнопку «{BTN_OPEN}»."
            else:
                ok, detail = await self.controller.open_gate(f"Telegram ({who})", force=True)
                answer = "Ворота открываются" if ok else f"Ошибка: {detail}"
                if ok and ev_id.isdigit():
                    self.db.update_event(int(ev_id), decision="manual", detail=f"открыто из Telegram: {who}")
                await self.broadcast(f"{'🔓' if ok else '⚠️'} {answer} — {who}")
        elif action == "add" and ev_id.isdigit():
            ev = self.db.get_event(int(ev_id))
            if ev and ev["plate"]:
                self.db.add_plate(ev["plate"], "", f"добавлен из Telegram ({who})")
                answer = f"{display(ev['plate'])} добавлен в список"
                await self.broadcast(f"➕ {answer} — {who}\nВладельца можно указать: /add {display(ev['plate']).replace(' ', '')} Имя")
        try:
            await self._call("answerCallbackQuery", data={"callback_query_id": q["id"], "text": answer[:190]})
        except Exception:
            pass

    # --- контроль агента, камер и внешний сторож -------------------------------------
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
        if not self.enabled or self.offline_since is not None:
            return  # без интернета предупреждения всё равно не уйдут — проверим после восстановления
        await self._alert("agent", not self.hub.online, self.hub.disconnected_at, self.tg.agent_alert_after,
                          "Агент ворот не на связи — автоматическое открытие не работает. "
                          "Проверьте, запущен ли агент на ПК и подключено ли реле.",
                          "Агент ворот снова на связи")
        for w in self.workers.values():
            st = w.status()
            down_since = w.reader.frame_ts or self.started_at
            await self._alert(f"cam:{w.cam.id}", not st["connected"], down_since, self.tg.camera_alert_after,
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
