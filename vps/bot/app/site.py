"""Данные об объекте на VPS.

ПК у ворот раз в ~10 секунд присылает POST /api/site/sync: состояние, копию
списка номеров, последние события и результаты применённых правок. В ответ
получает правки, которые ждут применения (/add, /del из Битрикс24).

Основной список номеров хранится на ПК. Здесь только копия для ответа на
«📋 Номера» и очередь правок. Всё сохраняется в data/site.json, чтобы
перезапуск бота не терял очередь.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path


class SiteState:
    def __init__(self, data_dir: Path, online_timeout: float = 45, op_ttl: float = 7 * 86400):
        self.file = data_dir / "site.json"
        self.online_timeout = online_timeout
        self.op_ttl = op_ttl
        self.data = {"last_sync": 0.0, "site": "", "status": "", "plates": [], "last_events": "",
                     "ops": [], "first_seen": 0.0}
        try:
            self.data.update(json.loads(self.file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

    def save(self):
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.file)

    # --- состояние ---------------------------------------------------------------------
    @property
    def last_sync(self) -> float:
        return self.data["last_sync"]

    @property
    def online(self) -> bool:
        return time.time() - self.last_sync <= self.online_timeout

    @property
    def ops(self) -> list[dict]:
        return self.data["ops"]

    def sync(self, payload: dict) -> tuple[list[dict], list[tuple[dict, str]]]:
        """Принимает данные с объекта. Возвращает (правки для объекта, выполненные правки с ответом)."""
        now = time.time()
        results = {r["id"]: r.get("text", "") for r in payload.get("results", []) if r.get("id")}
        done = [(op, results[op["id"]]) for op in self.ops if op["id"] in results]
        expired = [(op, "⚠️ Не выполнено: объект не выходил на связь") for op in self.ops
                   if op["id"] not in results and now - op["ts"] > self.op_ttl]
        finished = {op["id"] for op, _ in done + expired}
        self.data["ops"] = [op for op in self.ops if op["id"] not in finished]
        self.data.update(
            last_sync=now,
            site=payload.get("site", ""),
            status=payload.get("status", ""),
            plates=payload.get("plates", []),
            last_events=payload.get("last_events", ""),
        )
        if not self.data["first_seen"]:
            self.data["first_seen"] = now
        self.save()
        return [{k: op[k] for k in ("id", "cmd", "args", "who")} for op in self.ops], done + expired

    def add_op(self, cmd: str, args: str, who: str, dialog: str) -> dict:
        op = {"id": uuid.uuid4().hex[:12], "cmd": cmd, "args": args, "who": who, "dialog": dialog, "ts": time.time()}
        self.ops.append(op)
        self.save()
        return op
