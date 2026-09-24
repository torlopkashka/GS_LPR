"""Обмен данными ПК у ворот с ботом Битрикс24 на VPS.

Постоянного соединения нет: ПК сам раз в sync_interval секунд отправляет на
VPS обычный HTTP-запрос (POST /api/site/sync) с состоянием объекта, копией
списка номеров и последними событиями. В ответ приходят правки списка,
которые сотрудники отправили боту (/add, /del). ПК применяет их у себя и
в следующем запросе сообщает результат, бот пересылает его сотруднику.

Уведомления о проездах (с фото) уходят отдельным запросом POST /api/site/notify.

Бот на VPS воротами не управляет: открыть ворота из Битрикс24 нельзя.
Основной список номеров хранится здесь, на ПК, поэтому без интернета
ворота открываются как обычно.

Здесь же контроль агента ворот и камер (предупреждения) и сигнал
внешнему сторожу Uptime Kuma.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from pathlib import Path

import httpx

from .config import Config
from .plates import display, normalize

log = logging.getLogger("lpr.vps")

TITLES = {
    "granted": "✅ Проезд разрешён",
    "denied": "⛔ Неизвестный номер",
    "logged": "📷 Номер распознан",
    "error": "⚠️ Не удалось открыть ворота",
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


class VpsSync:
    def __init__(self, cfg: Config, db, hub, workers: dict, cameras: dict):
        self.cfg = cfg
        self.vc = cfg.vps
        self.db = db
        self.hub = hub
        self.workers = workers
        self.cameras = cameras
        self.started_at = time.time()
        self.client = httpx.AsyncClient(timeout=20)
        self.http = httpx.AsyncClient(timeout=10)  # для Uptime Kuma
        # связь с VPS
        self.last_ok = 0.0
        self.offline_since: float | None = time.time()
        self.last_outage: tuple[float, float] | None = None
        self.last_error = ""
        self._synced_once = False
        # результаты применённых правок: id -> текст (отправляются, пока VPS их не подтвердит)
        self._results: dict[str, str] = {}
        self._alerted: dict[str, float] = {}
        self._wake = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return bool(self.vc.url and self.vc.token)

    @property
    def online(self) -> bool:
        return self.offline_since is None

    # --- HTTP ------------------------------------------------------------------------
    async def _post(self, path: str, payload: dict) -> dict:
        r = await self.client.post(self.vc.url.rstrip("/") + path, json=payload,
                                   headers={"Authorization": f"Bearer {self.vc.token}"})
        r.raise_for_status()
        return r.json()

    # --- уведомления ------------------------------------------------------------------
    async def notify(self, text: str, buttons: list[dict] | None = None, photo: Path | None = None):
        """buttons=None — стандартное меню бота, [] — без кнопок."""
        if not self.enabled or not self.online:
            return  # без связи не копим: о пропущенном расскажет отчёт о восстановлении
        payload = {"text": text}
        if buttons is not None:
            payload["buttons"] = buttons
        if photo and photo.exists():
            payload["photo"] = {"name": photo.name, "b64": base64.b64encode(photo.read_bytes()).decode()}
        try:
            await self._post("/api/site/notify", payload)
        except Exception as e:
            log.warning("Не удалось отправить уведомление на VPS: %s", e)

    async def event(self, ev: dict | None, snapshot: Path | None):
        """Уведомление о проезде (вызывает AccessController)."""
        if not self.enabled or ev is None:
            return
        d = ev["decision"]
        if (d == "granted" and not self.vc.notify_granted) or (d == "denied" and not self.vc.notify_denied):
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
        if d in ("denied", "logged") and ev["plate"] and self.vc.add_button:
            buttons = [{"text": "➕ В список", "command": "add", "params": ev["plate"]}]
        await self.notify("\n".join(lines), buttons, snapshot)

    # --- тексты для бота --------------------------------------------------------------
    def status_text(self) -> str:
        now = time.time()
        lines = [f"🟢 Сервер у ворот работает {fmt_duration(now - self.started_at)}"]
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
        out = []
        for e in events:
            cam = self.cameras.get(e["camera"])
            who = f" — {e['owner']}" if e["owner"] else ""
            plate = display(e["plate"]) if e["plate"] else (e["detail"] or "—")
            out.append(f"{fmt_time(e['ts'])} {plate}{who} · {DECISION_SHORT.get(e['decision'], e['decision'])}"
                       + (f" · {cam.name}" if cam else ""))
        return "\n".join(out)

    def plates_list(self) -> list[dict]:
        return [{"plate": display(p["plate"]), "owner": p["owner"], "active": bool(p["active"]),
                 "valid_until": p["valid_until"]} for p in self.db.list_plates()]

    # --- правки списка из Битрикс24 ------------------------------------------------------
    def apply_op(self, op: dict) -> str:
        cmd, args, who = op.get("cmd"), (op.get("args") or "").strip(), op.get("who") or "?"
        if cmd == "add":
            return self._cmd_add(args, who)
        if cmd == "del":
            return self._cmd_del(args)
        return f"Неизвестная команда: {cmd}"

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
        self.db.add_plate(plate, owner, f"добавлен из Битрикс24 ({who})", valid)
        log.info("Номер %s добавлен из Битрикс24 (%s)", plate, who)
        return (f"✅ {display(plate)} добавлен" + (f" ({owner})" if owner else "")
                + (f", до {valid}" if valid else "") + f" — {who}")

    def _cmd_del(self, args: str) -> str:
        plate = normalize(args)
        row = self.db.find_plate(plate) if plate else None
        if not row:
            return f"Номер {args or '—'} не найден в списке"
        self.db.delete_plate(row["id"])
        log.info("Номер %s удалён из Битрикс24", plate)
        return f"🗑 {display(plate)} удалён"

    # --- обмен с VPS ----------------------------------------------------------------
    async def run(self):
        if not self.enabled:
            log.warning("Связь с VPS не настроена (VPS_URL / VPS_TOKEN) — уведомлений в Битрикс24 не будет")
            return
        while True:
            try:
                await self.sync_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._mark_offline(e)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self.vc.sync_interval)
            except asyncio.TimeoutError:
                pass

    async def sync_once(self):
        payload = {
            "site": self.vc.site_name,
            "status": self.status_text(),
            "plates": self.plates_list(),
            "last_events": self.last_events_text(),
            "results": [{"id": k, "text": v} for k, v in self._results.items()],
        }
        res = await self._post("/api/site/sync", payload)
        was_offline_since = self.offline_since if self._synced_once else None  # старт — не обрыв
        self._synced_once = True
        self._mark_online()
        # VPS больше не присылает подтверждённые результаты
        pending = {op["id"] for op in res.get("ops", [])}
        for k in list(self._results):
            if k not in pending:
                del self._results[k]
        applied = False
        for op in res.get("ops", []):
            if op["id"] in self._results:
                continue  # уже применено, ждём подтверждения
            try:
                self._results[op["id"]] = self.apply_op(op)
            except Exception as e:
                log.exception("Ошибка применения правки %s", op)
                self._results[op["id"]] = f"⚠️ Ошибка на объекте: {e}"
            applied = True
        if applied:
            self._wake.set()  # сразу сообщить результат
        if was_offline_since is not None:
            await self._report_outage(was_offline_since)

    def _mark_offline(self, err: Exception):
        self.last_error = str(err)
        if self.offline_since is None:
            self.offline_since = self.last_ok or time.time()
            log.warning("Нет связи с VPS: %s", err)

    def _mark_online(self):
        self.last_ok = time.time()
        if self.offline_since is not None:
            log.info("Связь с VPS установлена")
        self.offline_since = None
        self.last_error = ""

    async def _report_outage(self, since: float):
        now = time.time()
        if now - since < self.vc.outage_min:
            return
        self.last_outage = (since, now)
        self.db.add_event(camera="-", decision="system", detail=f"нет связи с VPS {fmt_duration(now - since)}")
        counts = self.db.count_events(since, now)
        parts = []
        for key, name in (("granted", "открыто по номеру"), ("denied", "отказов"), ("error", "ошибок открытия")):
            if counts.get(key):
                parts.append(f"{name}: {counts[key]}")
        await self.notify(
            "[B]🔌 Связь с объектом восстановлена[/B]\n"
            f"Связи не было {fmt_duration(now - since)}: с {fmt_time(since)} по {fmt_time(now)}.\n"
            "Ворота всё это время работали: распознавание и агент не зависят от интернета.\n"
            "За это время " + (", ".join(parts) if parts else "проездов не было") + ".")

    # --- контроль агента, камер и сторож Uptime Kuma ------------------------------
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
        if not self.enabled or not self.online:
            return  # без связи предупреждения всё равно не уйдут — проверим после восстановления
        await self._alert("agent", not self.hub.online, self.hub.disconnected_at, self.vc.agent_alert_after,
                          "Агент ворот не на связи — автоматическое открытие не работает. "
                          "Проверьте, запущен ли агент на ПК и подключено ли реле.",
                          "Агент ворот снова на связи")
        for w in self.workers.values():
            st = w.status()
            down_since = w.reader.frame_ts or self.started_at
            await self._alert(f"cam:{w.cam.id}", not st["connected"], down_since, self.vc.camera_alert_after,
                              f"Нет видео с камеры «{w.cam.name}»", f"Камера «{w.cam.name}» снова работает")

    async def _ping_healthcheck(self):
        """Сигнал Uptime Kuma (адрес содержит /api/push/) или healthchecks.io."""
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
