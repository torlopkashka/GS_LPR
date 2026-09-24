"""Настройки бота на VPS. Всё задаётся переменными окружения (файл vps/.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _ids(value: str) -> list[int]:
    return [int(x) for x in value.replace(" ", "").split(",") if x]


@dataclass
class BotConfig:
    # Входящий вебхук Битрикс24 с правами imbot: https://портал.bitrix24.ru/rest/1/xxxxxxxx/
    webhook_url: str = ""
    # Секрет бота (до 40 символов). Не меняйте после первого запуска
    bot_token: str = ""
    bot_code: str = "gs_lpr_gate"
    bot_name: str = "Ворота"
    # ID сотрудников Битрикс24, которым разрешено управлять воротами
    user_ids: list[int] = field(default_factory=list)
    # Куда слать уведомления: "chat123" или ID сотрудника. Пусто — бот создаст чат «Ворота»
    dialog_id: str = ""
    poll_interval: float = 3.0
    # Общий секрет связи с ПК у ворот (LINK_TOKEN в local/.env должен совпадать)
    link_token: str = ""
    # «Открыть», нажатое больше N секунд назад, не выполняется
    max_command_age: float = 120
    # Сколько ждать ответа от ПК у ворот на команду
    command_timeout: float = 15
    # Сообщить в чат, если объект не на связи дольше N секунд (0 — не сообщать)
    site_offline_alert_after: float = 180
    # Обрыв связи короче N секунд не считается
    outage_min: float = 60
    data_dir: Path = Path("data")


def load_config() -> BotConfig:
    e = os.environ.get
    cfg = BotConfig(
        webhook_url=e("B24_WEBHOOK_URL", ""),
        bot_token=e("B24_BOT_TOKEN", ""),
        bot_name=e("B24_BOT_NAME", "Ворота"),
        user_ids=_ids(e("B24_USER_IDS", "")),
        dialog_id=e("B24_DIALOG_ID", ""),
        link_token=e("LINK_TOKEN", ""),
        max_command_age=float(e("MAX_COMMAND_AGE", "120")),
        site_offline_alert_after=float(e("SITE_OFFLINE_ALERT_AFTER", "180")),
        outage_min=float(e("OUTAGE_MIN", "60")),
        data_dir=Path(e("DATA_DIR", "data")),
    )
    if not cfg.link_token:
        raise RuntimeError("Не задан LINK_TOKEN (см. vps/.env.example)")
    if len(cfg.bot_token) > 40:
        raise RuntimeError("B24_BOT_TOKEN должен быть не длиннее 40 символов")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
