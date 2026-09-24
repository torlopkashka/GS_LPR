"""Локальная сторона связи с VPS: команды из Битрикс24 и уведомления."""

import asyncio
import base64
import json
import time

import pytest

from app.cloudlink import CloudLink
from app.config import Config, GateConfig, LinkConfig, RecognitionConfig
from app.db import Database
from app.gate import AgentHub


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class FakeController:
    def __init__(self):
        self.opens = 0

    async def open_gate(self, reason, force=False):
        self.opens += 1
        return True, "открыто"


@pytest.fixture()
def link(tmp_path):
    cfg = Config(cameras=[], recognition=RecognitionConfig(), gate=GateConfig(), data_dir=tmp_path,
                 link=LinkConfig(url="wss://vps.example.ru/link", token="t"))
    lk = CloudLink(cfg, Database(tmp_path / "t.db"), AgentHub(), {}, {})
    lk.controller = FakeController()
    lk.ws = FakeWS()
    return lk


def run(coro):
    return asyncio.run(coro)


def cmd(lk, name, params="", **extra):
    return run(lk.handle_command({"cmd": name, "params": params, "who": "Аня", **extra}))


def test_open(link):
    assert "Ворота открываются — Аня" in cmd(link, "open")
    assert link.controller.opens == 1
    assert link.db.list_events(limit=1)[0]["decision"] == "manual"


def test_open_from_old_notification_refused(link):
    ev = link.db.add_event(camera="c", plate="K555MM99", decision="denied", ts=time.time() - 3600)
    assert "устарело" in cmd(link, "open", f"ev:{ev}")
    assert link.controller.opens == 0


def test_open_from_notification_marks_event(link):
    ev = link.db.add_event(camera="c", plate="K555MM99", decision="denied")
    cmd(link, "open", f"ev:{ev}")
    assert link.db.get_event(ev)["decision"] == "manual"


def test_add_list_del(link):
    assert "добавлен" in cmd(link, "add", "а123вс77 Иван Петров до 31.12.2030")
    p = link.db.find_plate("A123BC77")
    assert p["owner"] == "Иван Петров" and p["valid_until"] == "2030-12-31"
    assert "А123ВС 77 — Иван Петров" in cmd(link, "list")
    assert "удалён" in cmd(link, "del", "А123ВС77")
    assert link.db.find_plate("A123BC77") is None


def test_add_from_notification(link):
    ev = link.db.add_event(camera="c", plate="K555MM99", decision="denied")
    assert "К555ММ 99 добавлен" in cmd(link, "add", f"ev:{ev}")
    assert link.db.find_plate("K555MM99")


def test_status_and_outage_summary(link):
    assert "Агент ворот: НЕ на связи" in cmd(link, "status")
    link.db.add_event(camera="c", plate="A123BC77", decision="granted", ts=time.time() - 100)
    text = cmd(link, "outage_summary", since=time.time() - 600, until=time.time())
    assert "открыто по номеру: 1" in text


def test_notification_with_photo_and_buttons(link, tmp_path):
    snap = tmp_path / "s.jpg"
    snap.write_bytes(b"\xff\xd8jpeg")
    ev = link.db.add_event(camera="c", plate="K555MM99", decision="denied")
    run(link.event(link.db.get_event(ev), snap))
    msg = link.ws.sent[-1]
    assert msg["type"] == "notify" and "Неизвестный номер: К555ММ 99" in msg["text"]
    assert base64.b64decode(msg["photo"]["b64"]) == b"\xff\xd8jpeg"
    assert [(b["command"], b["params"]) for b in msg["buttons"]] == [("open", f"ev:{ev}"), ("add", f"ev:{ev}")]


def test_granted_notification_uses_default_menu(link):
    ev = link.db.add_event(camera="c", plate="A123BC77", decision="granted")
    run(link.event(link.db.get_event(ev), None))
    assert "buttons" not in link.ws.sent[-1]


def test_reply_protocol(link):
    run(link._reply({"type": "command", "id": "abc", "cmd": "list"}))
    assert link.ws.sent[-1] == {"type": "result", "id": "abc", "text": "Список пуст. Добавить: /add А123ВС77 Владелец"}


def test_no_sends_while_offline(link):
    link.ws = None
    assert run(link._send({"type": "notify"})) is False


def test_agent_alerts(link):
    link.hub.disconnected_at = time.time() - 100
    run(link._check_alerts())
    run(link._check_alerts())
    alerts = [m for m in link.ws.sent if "Агент ворот не на связи" in m.get("text", "")]
    assert len(alerts) == 1
    link.hub.ws = object()
    run(link._check_alerts())
    assert "снова на связи" in link.ws.sent[-1]["text"]


class FakeHttp:
    def __init__(self):
        self.calls = []

    async def get(self, url, params=None, **kw):
        self.calls.append(("GET", url, params))

    async def post(self, url, content=None, **kw):
        self.calls.append(("POST", url, content))


def test_healthcheck_formats(link):
    link.http = FakeHttp()
    link.cfg.healthcheck.url = "https://vps.example.ru/api/push/AbC123?status=up&msg=OK&ping="
    run(link._ping_healthcheck())
    method, url, params = link.http.calls[-1]
    assert method == "GET" and url == "https://vps.example.ru/api/push/AbC123"
    assert params["status"] == "down" and "агент" in params["msg"]
    link.hub.ws = object()
    run(link._ping_healthcheck())
    assert link.http.calls[-1][2]["status"] == "up"
    link.cfg.healthcheck.url = "https://hc-ping.com/uuid"
    link.hub.ws = None
    run(link._ping_healthcheck())
    assert link.http.calls[-1][1] == "https://hc-ping.com/uuid/fail"
