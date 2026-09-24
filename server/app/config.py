"""Загрузка конфигурации: config.yaml + секреты из переменных окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class CameraConfig:
    id: str
    name: str
    url: str
    # entry — камера снаружи (въезд), exit — изнутри (выезд)
    role: str = "entry"
    enabled: bool = True
    # whitelist — открывать только разрешённым; any — любому распознанному номеру;
    # none — только журнал
    open_policy: str = "whitelist"
    # Зона интереса [x1, y1, x2, y2] в долях кадра (0..1). Обрезка ускоряет работу
    # и отсекает номера машин на дороге.
    roi: list[float] | None = None
    process_fps: float = 4.0
    # Раз в сколько секунд анализировать кадр при отсутствии движения (0 — никогда)
    idle_interval: float = 2.0
    motion_threshold: float = 0.004


@dataclass
class RecognitionConfig:
    detector_model: str = "yolo-v9-t-384-license-plate-end2end"
    ocr_model: str = "cct-xs-v2-global-model"
    detector_conf: float = 0.4
    # ru — только российские номера (с исправлением ошибок по позициям),
    # ru_or_any — российские исправляются, остальные принимаются как есть, any — любые
    plate_format: str = "ru"
    min_ocr_conf: float = 0.6
    # Сколько раз номер должен быть прочитан одинаково, чтобы открыть ворота
    min_confirmations: int = 2
    # Допустимое число ошибок при сравнении со списком (0 — только точное совпадение)
    max_distance: int = 0
    # Пауза без чтений, после которой «проезд» считается завершённым
    session_gap: float = 2.5
    onnx_threads: int = 1
    save_crops: bool = True


@dataclass
class GateConfig:
    pulse_seconds: float = 1.0
    # Минимальный интервал между командами на ворота (защита от «двойного» импульса)
    min_interval: float = 8.0
    # Повторно не открывать для того же номера в течение N секунд (с обеих камер)
    plate_cooldown: float = 60.0
    ack_timeout: float = 5.0


@dataclass
class Bitrix24Config:
    # Входящий вебхук Битрикс24 с правами imbot: https://портал.bitrix24.ru/rest/1/xxxxxxxx/
    webhook_url: str = ""
    # Секрет бота (до 40 символов). Задаётся один раз, при смене бот регистрируется заново
    bot_token: str = ""
    bot_code: str = "gs_lpr_gate"
    bot_name: str = "Ворота"
    # ID сотрудников Битрикс24, которым разрешено управлять воротами
    user_ids: list[int] = field(default_factory=list)
    # Куда слать уведомления: "chat123" — групповой чат, "15" — личный диалог.
    # Пусто — бот сам создаст групповой чат «Ворота» с сотрудниками из user_ids
    dialog_id: str = ""
    poll_interval: float = 3.0
    notify_granted: bool = True
    notify_denied: bool = True
    # Кнопки «Открыть» / «Добавить в список» под уведомлением о неизвестном номере
    interactive: bool = True
    # Команда «открыть», нажатая больше N секунд назад (например, пока не было
    # интернета), не выполняется
    max_command_age: float = 120
    # Кнопку «Открыть» под уведомлением можно нажать не позже, чем через N секунд
    max_callback_age: float = 900
    # Обрыв связи короче N секунд не считается (в отчёт о восстановлении не попадает)
    outage_min: float = 60
    # Предупреждать, если агент ворот / камера не на связи дольше N секунд
    agent_alert_after: float = 30
    camera_alert_after: float = 120


@dataclass
class HealthcheckConfig:
    # Внешний «сторож» (Uptime Kuma или healthchecks.io): сервер раз в interval секунд
    # отправляет сюда запрос. Если запросы прекратились (пропал интернет или
    # выключился ПК), сторож сам пришлёт уведомление (Битрикс24, SMS, почта).
    url: str = ""
    interval: float = 60


@dataclass
class Config:
    cameras: list[CameraConfig]
    recognition: RecognitionConfig
    gate: GateConfig
    bitrix24: Bitrix24Config
    data_dir: Path
    healthcheck: HealthcheckConfig = field(default_factory=HealthcheckConfig)
    retention_days: int = 30
    admin_user: str = "admin"
    admin_password: str = ""
    secret_key: str = ""
    agent_token: str = ""
    api_token: str = ""


def _section(cls, data: dict | None):
    data = data or {}
    known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
    return cls(**known)


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("LPR_CONFIG", "config.yaml"))
    raw = yaml.safe_load(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    raw = raw or {}

    cameras = [_section(CameraConfig, c) for c in raw.get("cameras", [])]
    b24 = _section(Bitrix24Config, raw.get("bitrix24"))
    b24.webhook_url = os.environ.get("B24_WEBHOOK_URL") or b24.webhook_url
    b24.bot_token = os.environ.get("B24_BOT_TOKEN") or b24.bot_token
    b24.dialog_id = os.environ.get("B24_DIALOG_ID") or b24.dialog_id
    if os.environ.get("B24_USER_IDS"):
        b24.user_ids = [int(x) for x in os.environ["B24_USER_IDS"].split(",") if x.strip()]
    if len(b24.bot_token) > 40:
        raise RuntimeError("B24_BOT_TOKEN должен быть не длиннее 40 символов")

    healthcheck = _section(HealthcheckConfig, raw.get("healthcheck"))
    healthcheck.url = os.environ.get("HEALTHCHECK_URL", healthcheck.url)

    cfg = Config(
        healthcheck=healthcheck,
        cameras=cameras,
        recognition=_section(RecognitionConfig, raw.get("recognition")),
        gate=_section(GateConfig, raw.get("gate")),
        bitrix24=b24,
        data_dir=Path(os.environ.get("LPR_DATA_DIR", raw.get("data_dir", "data"))),
        retention_days=int(raw.get("retention_days", 30)),
        admin_user=os.environ.get("ADMIN_USER", "admin"),
        admin_password=os.environ.get("ADMIN_PASSWORD", ""),
        secret_key=os.environ.get("SECRET_KEY", ""),
        agent_token=os.environ.get("AGENT_TOKEN", ""),
        api_token=os.environ.get("API_TOKEN", ""),
    )
    for name in ("admin_password", "secret_key", "agent_token"):
        if not getattr(cfg, name):
            raise RuntimeError(f"Не задана переменная окружения {name.upper()} (см. .env.example)")
    ids = [c.id for c in cameras]
    if len(ids) != len(set(ids)):
        raise RuntimeError("Идентификаторы камер (id) должны быть уникальными")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
