#!/usr/bin/env python3
"""Агент управления воротами.

Запускается на том же компьютере, что и сервер распознавания, но не в Docker,
а прямо в Windows: Docker Desktop не даёт контейнерам доступа к USB-реле.
Держит WebSocket-соединение с сервером (ws://127.0.0.1:8000) и по команде
«open» замыкает реле на заданное время. Интернет для этого не нужен.

Поддерживаемые способы управления (параметр driver в agent.yaml):
  serial   — USB-реле на CH340 (LCUS-1/2/4 и аналоги, «виртуальный COM-порт»)
  hid      — USB HID-реле (USBRelay1/2, «dcttech», VID 16c0 PID 05df)
  http     — сетевое реле (Shelly, Sonoff в режиме DIY, ESPHome, Nice IT4WIFI через шлюз)
  gpio     — выход GPIO Raspberry Pi (через модуль реле)
  command  — произвольная команда оболочки
  dummy    — только запись в журнал (для проверки связи)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import platform
import socket
import subprocess
import time
import urllib.request

import yaml

log = logging.getLogger("gate-agent")
VERSION = "1.0"


# --------------------------------------------------------------------------
# Драйверы реле
# --------------------------------------------------------------------------
class Driver:
    name = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def pulse(self, seconds: float) -> None:
        self.on()
        try:
            time.sleep(seconds)
        finally:
            self.off()

    def on(self) -> None:
        raise NotImplementedError

    def off(self) -> None:
        raise NotImplementedError


class DummyDriver(Driver):
    name = "dummy"

    def on(self):
        log.info("[dummy] реле ВКЛ")

    def off(self):
        log.info("[dummy] реле ВЫКЛ")


class SerialDriver(Driver):
    """USB-реле LCUS на CH340: команда A0 <канал> <0|1> <контрольная сумма>."""

    name = "serial"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        import serial  # pyserial

        self.serial = serial
        self.port = cfg.get("port", "/dev/ttyUSB0")
        self.baud = int(cfg.get("baudrate", 9600))
        self.channel = int(cfg.get("channel", 1))

    def _send(self, state: int):
        cmd = bytes([0xA0, self.channel, state, (0xA0 + self.channel + state) & 0xFF])
        with self.serial.Serial(self.port, self.baud, timeout=1) as s:
            s.write(cmd)
            s.flush()

    def on(self):
        self._send(1)

    def off(self):
        self._send(0)


class HidDriver(Driver):
    """USB HID-реле (usbrelay/dcttech): feature report 0xFF — вкл, 0xFD — выкл."""

    name = "hid"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        import hid  # пакет hidapi

        self.hid = hid
        self.vid = int(str(cfg.get("vendor_id", "0x16c0")), 0)
        self.pid = int(str(cfg.get("product_id", "0x05df")), 0)
        self.channel = int(cfg.get("channel", 1))

    def _send(self, code: int):
        dev = self.hid.device()
        dev.open(self.vid, self.pid)
        try:
            dev.send_feature_report([0x00, code, self.channel, 0, 0, 0, 0, 0, 0])
        finally:
            dev.close()

    def on(self):
        self._send(0xFF)

    def off(self):
        self._send(0xFD)


class HttpDriver(Driver):
    """Сетевое реле.

    Если задан pulse_url — отправляется один запрос (реле само выключится,
    например Shelly с toggle_after), иначе on_url, пауза, off_url.
    """

    name = "http"

    def _req(self, url: str):
        method = self.cfg.get("method", "GET").upper()
        data = self.cfg.get("body")
        req = urllib.request.Request(url, method=method, data=data.encode() if data else None)
        for k, v in (self.cfg.get("headers") or {}).items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=float(self.cfg.get("timeout", 5))) as r:
            if r.status >= 300:
                raise RuntimeError(f"HTTP {r.status}")

    def pulse(self, seconds: float):
        if self.cfg.get("pulse_url"):
            self._req(self.cfg["pulse_url"])
        else:
            super().pulse(seconds)

    def on(self):
        self._req(self.cfg["on_url"])

    def off(self):
        if self.cfg.get("off_url"):
            self._req(self.cfg["off_url"])


class GpioDriver(Driver):
    name = "gpio"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        from gpiozero import OutputDevice

        self.dev = OutputDevice(int(cfg.get("pin", 17)), active_high=bool(cfg.get("active_high", True)),
                                initial_value=False)

    def on(self):
        self.dev.on()

    def off(self):
        self.dev.off()


class CommandDriver(Driver):
    name = "command"

    def _run(self, key: str):
        cmd = self.cfg.get(key)
        if cmd:
            subprocess.run(cmd, shell=True, check=True, timeout=10)

    def pulse(self, seconds: float):
        if self.cfg.get("pulse_cmd"):
            self._run("pulse_cmd")
        else:
            super().pulse(seconds)

    def on(self):
        self._run("on_cmd")

    def off(self):
        self._run("off_cmd")


DRIVERS = {d.name: d for d in (DummyDriver, SerialDriver, HidDriver, HttpDriver, GpioDriver, CommandDriver)}


def make_driver(cfg: dict) -> Driver:
    name = cfg.get("driver", "dummy")
    if name not in DRIVERS:
        raise SystemExit(f"Неизвестный драйвер {name!r}. Доступны: {', '.join(DRIVERS)}")
    return DRIVERS[name](cfg.get(name) or {})


# --------------------------------------------------------------------------
# Связь с сервером
# --------------------------------------------------------------------------
class Agent:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.url = cfg["server_url"].rstrip("/")
        if not self.url.endswith("/ws/agent"):
            self.url += "/ws/agent"
        self.token = cfg["token"]
        self.driver = make_driver(cfg)
        self.max_pulse = float(cfg.get("max_pulse", 3))
        self.lock = asyncio.Lock()

    async def handle_open(self, msg: dict) -> dict:
        pulse = min(float(msg.get("pulse", 1)), self.max_pulse)
        async with self.lock:
            try:
                await asyncio.to_thread(self.driver.pulse, pulse)
                log.info("Ворота: импульс %.1f с", pulse)
                return {"type": "ack", "id": msg.get("id"), "ok": True}
            except Exception as e:
                log.exception("Ошибка управления реле")
                return {"type": "ack", "id": msg.get("id"), "ok": False, "error": str(e)}

    async def run(self):
        from websockets.asyncio.client import connect

        backoff = 2
        while True:
            try:
                async with connect(
                    self.url,
                    additional_headers={"Authorization": f"Bearer {self.token}"},
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=15,
                ) as ws:
                    log.info("Подключено к %s", self.url)
                    backoff = 2
                    await ws.send(json.dumps({
                        "type": "hello", "name": self.cfg.get("name") or socket.gethostname(),
                        "driver": self.driver.name, "version": VERSION, "platform": platform.platform(),
                    }))
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("type") == "open":
                            await ws.send(json.dumps(await self.handle_open(msg)))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Нет связи с сервером: %s. Повтор через %d с", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def main():
    ap = argparse.ArgumentParser(description="Агент управления воротами")
    ap.add_argument("-c", "--config", default="agent.yaml")
    ap.add_argument("--test", action="store_true", help="подать один импульс и выйти")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--log-file", help="писать журнал в файл (с ротацией)")
    args = ap.parse_args()
    handlers = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.handlers.RotatingFileHandler(
            args.log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    with open(args.config, encoding="utf-8-sig") as f:
        cfg = yaml.safe_load(f)
    if args.test:
        drv = make_driver(cfg)
        log.info("Тестовый импульс через драйвер %s", drv.name)
        drv.pulse(float(cfg.get("test_pulse", 1)))
        return
    asyncio.run(Agent(cfg).run())


if __name__ == "__main__":
    main()
