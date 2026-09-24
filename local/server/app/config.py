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
class VpsConfig:
    """Обмен с ботом Битрикс24 на VPS (уведомления и правки списка номеров)."""
    # Адрес бота на VPS: http://IP-VPS:8080
    url: str = ""
    # Общий секрет, должен совпадать с SITE_TOKEN в vps/.env
    token: str = ""
    site_name: str = "Ворота"
    # Как часто обмениваться данными с VPS, секунд
    sync_interval: float = 10
    notify_granted: bool = True
    notify_denied: bool = True
    # Кнопка «В список» под уведомлением о неизвестном номере
    add_button: bool = True
    # Обрыв связи короче N секунд не считается (отчёт о восстановлении не отправляется)
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
    vps: VpsConfig
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
    vps = _section(VpsConfig, raw.get("vps"))
    vps.url = os.environ.get("VPS_URL") or vps.url
    vps.token = os.environ.get("VPS_TOKEN") or vps.token

    healthcheck = _section(HealthcheckConfig, raw.get("healthcheck"))
    healthcheck.url = os.environ.get("HEALTHCHECK_URL", healthcheck.url)

    cfg = Config(
        healthcheck=healthcheck,
        cameras=cameras,
        recognition=_section(RecognitionConfig, raw.get("recognition")),
        gate=_section(GateConfig, raw.get("gate")),
        vps=vps,
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
