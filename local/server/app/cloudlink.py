"""Связь ПК у ворот с ботом на VPS.

Сервер сам подключается к VPS по WebSocket (wss://домен/link) и держит
соединение. Белый IP на объекте не нужен. По соединению уходят уведомления
о проездах (с фото и кнопками), предупреждения и снимок состояния, а
приходят команды от сотрудников из Битрикс24: open, status, last, list,
add, del, outage_summary. Вся логика и данные — здесь, бот на VPS только
передаёт команды и показывает ответы.

Здесь же контроль агента ворот и камер (предупреждения) и сигнал внешнему
сторожу Uptime Kuma.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from pathlib import Path

import httpx

from .config import Config
from .plates import display, normalize

log = logging.getLogger("lpr.link")

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


class CloudLink:
    def __init__(self, cfg: Config, db, hub, workers: dict, cameras: dict):
        self.cfg = cfg
        self.lc = cfg.link
        self.db = db
        self.hub = hub
        self.workers = workers
        self.cameras = cameras
        self.controller = None  # AccessController, задаётся в main
        self.started_at = time.time()
        self.http = httpx.AsyncClient(timeout=10)  # для внешнего сторожа
        self.ws = None
        # состояние связи с VPS
        self.offline_since: float | None = time.time()
        self.last_outage: tuple[float, float] | None = None
        self._alerted: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.lc.url and self.lc.token)

    @property
    def online(self) -> bool:
        return self.ws is not None

    # --- отправка на VPS -------------------------------------------------------------
    async def _send(self, msg: dict) -> bool:
        ws = self.ws
        if ws is None:
            return False  # без связи не копим: о пропущенном расскажет отчёт о восстановлении
        try:
            await ws.send(json.dumps(msg))
            return True
        except Exception as e:
            log.warning("Не удалось отправить на VPS: %s", e)
            return False

    async def notify(self, text: str, buttons: list[dict] | None = None, photo: Path | None = None):
        """buttons=None — стандартное меню бота, [] — без кнопок."""
        msg = {"type": "notify", "text": text}
        if buttons is not None:
            msg["buttons"] = buttons
        if photo and photo.exists():
            msg["photo"] = {"name": photo.name, "b64": base64.b64encode(photo.read_bytes()).decode()}
        await self._send(msg)

    # --- уведомления о проездах (вызывает AccessController) ---------------------
    async def event(self, ev: dict | None, snapshot: Path | None):
        if not self.enabled or ev is None:
            return
        d = ev["decision"]
        if (d == "granted" and not self.lc.notify_granted) or (d == "denied" and not self.lc.notify_denied):
            return
        cam = self.cameras.get(ev["camera"])
        lines = [f"[B]{TITLES.get(d, d)}: {display(ev['plate'])}[/B]"]
        if ev.get("owner"):
            lines.append(f"Владелец: {ev['owner']}")
        lines.append(f"Камера: {cam.name if cam else ev['camera']}")
        if ev.get("detail") and d != "denied":
            lines.append(ev["detail"])
        lines.append(time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(ev["ts"])))
        buttons = None
        if d in ("denied", "logged", "error") and self.lc.interactive:
            buttons = [{"text": "🔓 Открыть ворота", "command": "open", "params": f"ev:{ev['id']}",
                        "color": "primary"}]
            if ev["plate"]:
                buttons.append({"text": "➕ В список", "command": "add", "params": f"ev:{ev['id']}"})
        await self.notify("\n".join(lines), buttons, snapshot)

    # --- тексты -------------------------------------------------------------------------
    def status_text(self) -> str:
        now = time.time()
        lines = [f"[B]📡 Состояние на {time.strftime('%d.%m.%Y %H:%M:%S')}[/B]",
                 f"🟢 Объект на связи, сервер работает {fmt_duration(now - self.started_at)}"]
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
            lines.append(f"🌐 Последний обрыв связи: {fmt_time(a)}–{fmt_time(b)} ({fmt_duration(b - a)})")
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

    def outage_summary(self, since: float, until: float) -> str:
        counts = self.db.count_events(since, until)
        parts = []
        for key, name in (("granted", "открыто по номеру"), ("denied", "отказов"),
                          ("manual", "открыто вручную"), ("error", "ошибок открытия")):
            if counts.get(key):
                parts.append(f"{name}: {counts[key]}")
        return ("Ворота всё это время работали: распознавание и агент не зависят от интернета.\n"
                "За это время " + (", ".join(parts) if parts else "проездов не было") + ".")

    # --- команды из Битрикс24 ----------------------------------------------------------
    async def handle_command(self, msg: dict) -> str:
        cmd, params, who = msg.get("cmd"), (msg.get("params") or "").strip(), msg.get("who") or "?"
        if cmd == "open":
            return await self._cmd_open(params, who)
        if cmd == "status":
            return self.status_text()
        if cmd == "last":
            return self.last_events_text()
        if cmd == "list":
            return self.plates_text()
        if cmd == "add":
            return self._cmd_add(params, who)
        if cmd == "del":
            return self._cmd_del(params)
        if cmd == "outage_summary":
            return self.outage_summary(float(msg.get("since", 0)), float(msg.get("until", time.time())))
        return f"Неизвестная команда: {cmd}"

    async def _cmd_open(self, params: str, who: str) -> str:
        ev = None
        if params.startswith("ev:") and params[3:].isdigit():
            ev = self.db.get_event(int(params[3:]))
            if ev and time.time() - ev["ts"] > self.lc.max_callback_age:
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

    # --- соединение с VPS ------------------------------------------------------------
    async def run(self):
        if not self.enabled:
            log.warning("Связь с VPS не настроена (VPS_LINK_URL / LINK_TOKEN) — уведомлений не будет")
            return
        from websockets.asyncio.client import connect

        backoff = 2
        while True:
            try:
                async with connect(self.lc.url, additional_headers={"Authorization": f"Bearer {self.lc.token}"},
                                   ping_interval=20, ping_timeout=20, open_timeout=15,
                                   max_size=16 * 1024 * 1024) as ws:
                    self.ws = ws
                    self._mark_online()
                    backoff = 2
                    await ws.send(json.dumps({"type": "hello", "site": self.lc.site_name}))
                    status_task = asyncio.create_task(self._status_loop())
                    try:
                        async for raw in ws:
                            msg = json.loads(raw)
                            if msg.get("type") == "command":
                                asyncio.create_task(self._reply(msg))
                    finally:
                        status_task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Нет связи с VPS: %s. Повтор через %d с", e, backoff)
            if self.ws is not None:
                self.ws = None
                self.offline_since = time.time()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _reply(self, msg: dict):
        try:
            text = await self.handle_command(msg)
        except Exception as e:
            log.exception("Ошибка выполнения команды %s", msg.get("cmd"))
            text = f"⚠️ Ошибка на объекте: {e}"
        await self._send({"type": "result", "id": msg.get("id"), "text": text})

    async def _status_loop(self):
        while True:
            await self._send({"type": "status", "text": self.status_text()})
            await asyncio.sleep(30)

    def _mark_online(self):
        now = time.time()
        since, self.offline_since = self.offline_since, None
        log.info("Связь с VPS установлена")
        if since is not None and now - since >= 60 and now - self.started_at > 60:
            self.last_outage = (since, now)
            self.db.add_event(camera="-", decision="system", detail=f"нет связи с VPS {fmt_duration(now - since)}")

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
            await self.notify(f"⚠️ {down_text} (с {fmt_time(since)})")
        elif not down and key in self._alerted:
            since = self._alerted.pop(key)
            await self.notify(f"✅ {up_text} (не было {fmt_duration(now - since)})")

    async def _check_alerts(self):
        if not self.online:
            return  # без связи предупреждения всё равно не уйдут — проверим после восстановления
        await self._alert("agent", not self.hub.online, self.hub.disconnected_at, self.lc.agent_alert_after,
                          "Агент ворот не на связи — автоматическое открытие не работает. "
                          "Проверьте, запущен ли агент на ПК и подключено ли реле.",
                          "Агент ворот снова на связи")
        for w in self.workers.values():
            st = w.status()
            down_since = w.reader.frame_ts or self.started_at
            await self._alert(f"cam:{w.cam.id}", not st["connected"], down_since, self.lc.camera_alert_after,
                              f"Нет видео с камеры «{w.cam.name}»", f"Камера «{w.cam.name}» снова работает")

    async def _ping_healthcheck(self):
        """Сигнал внешнему сторожу. Uptime Kuma (адрес содержит /api/push/) или healthchecks.io."""
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
