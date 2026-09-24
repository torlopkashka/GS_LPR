"""Обмен ПК у ворот с ботом на VPS (имитация VPS через httpx.MockTransport)."""

import asyncio
import base64
import json
import time

import httpx
import pytest

from app.config import Config, GateConfig, RecognitionConfig, VpsConfig
from app.db import Database
from app.gate import AgentHub
from app.vpssync import VpsSync


class FakeVps:
    def __init__(self):
        self.ops = []           # правки, которые ждут применения
        self.results = {}       # id -> текст результата
        self.syncs = []
        self.notifies = []
        self.down = False
        self.ignore_results = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("no internet")
        assert request.headers["authorization"] == "Bearer tok"
        body = json.loads(request.content)
        if request.url.path == "/api/site/sync":
            self.syncs.append(body)
            for r in ([] if self.ignore_results else body["results"]):
                self.results[r["id"]] = r["text"]
                self.ops = [o for o in self.ops if o["id"] != r["id"]]
            return httpx.Response(200, json={"ops": self.ops})
        if request.url.path == "/api/site/notify":
            self.notifies.append(body)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)


@pytest.fixture()
def env(tmp_path):
    vps = FakeVps()
    cfg = Config(cameras=[], recognition=RecognitionConfig(), gate=GateConfig(), data_dir=tmp_path,
                 vps=VpsConfig(url="http://203.0.113.10:8080", token="tok"))
    sync = VpsSync(cfg, Database(tmp_path / "t.db"), AgentHub(), {}, {})
    sync.client = httpx.AsyncClient(transport=httpx.MockTransport(vps.handler))
    return sync, vps


def run(coro):
    return asyncio.run(coro)


def test_sync_sends_state_and_applies_ops(env):
    sync, vps = env
    sync.db.add_plate("K555MM99", "Петров")
    vps.ops = [{"id": "1", "cmd": "add", "args": "а123вс77 Иван Петров до 31.12.2030", "who": "Аня"},
               {"id": "2", "cmd": "del", "args": "К555ММ99", "who": "Аня"}]
    run(sync.sync_once())
    first = vps.syncs[-1]
    assert "Агент ворот: НЕ на связи" in first["status"]
    assert first["plates"][0]["plate"] == "К555ММ 99"
    p = sync.db.find_plate("A123BC77")
    assert p["owner"] == "Иван Петров" and p["valid_until"] == "2030-12-31"
    assert sync.db.find_plate("K555MM99") is None
    # результат уходит следующим запросом, правки больше не применяются повторно
    run(sync.sync_once())
    assert "А123ВС 77 добавлен" in vps.results["1"] and "удалён" in vps.results["2"]
    assert vps.ops == []
    run(sync.sync_once())
    assert vps.syncs[-1]["results"] == []


def test_op_not_reapplied_when_result_lost(env):
    sync, vps = env
    sync.db.add_plate("K555MM99")
    vps.ops = [{"id": "7", "cmd": "del", "args": "K555MM99", "who": "Аня"}]
    run(sync.sync_once())
    result = sync._results["7"]
    vps.ignore_results = True
    # VPS снова присылает ту же правку (ответ потерялся) — повторно не применяется
    run(sync.sync_once())
    assert sync._results["7"] == result and "удалён" in result


def test_offline_and_outage_report(env):
    sync, vps = env
    run(sync.sync_once())
    assert sync.online
    vps.down = True
    with pytest.raises(httpx.ConnectError):
        run(sync.sync_once())
    sync._mark_offline(Exception("no internet"))
    assert not sync.online
    run(sync.notify("не уйдёт"))
    assert vps.notifies == []
    sync.offline_since = time.time() - 600
    sync.db.add_event(camera="c", plate="A123BC77", decision="granted", ts=time.time() - 300)
    vps.down = False
    run(sync.sync_once())
    report = vps.notifies[-1]["text"]
    assert "восстановлена" in report and "10 мин" in report and "открыто по номеру: 1" in report


def test_notification_with_photo_and_add_button(env, tmp_path):
    sync, vps = env
    run(sync.sync_once())
    snap = tmp_path / "s.jpg"
    snap.write_bytes(b"\xff\xd8jpeg")
    ev = sync.db.add_event(camera="c", plate="K555MM99", decision="denied")
    run(sync.event(sync.db.get_event(ev), snap))
    msg = vps.notifies[-1]
    assert "Неизвестный номер: К555ММ 99" in msg["text"]
    assert base64.b64decode(msg["photo"]["b64"]) == b"\xff\xd8jpeg"
    assert msg["buttons"] == [{"text": "➕ В список", "command": "add", "params": "K555MM99"}]
    # никаких кнопок открытия ворот
    assert all(b["command"] != "open" for b in msg["buttons"])


def test_granted_notification_default_menu(env):
    sync, vps = env
    run(sync.sync_once())
    ev = sync.db.add_event(camera="c", plate="A123BC77", decision="granted", owner="Иванов")
    run(sync.event(sync.db.get_event(ev), None))
    assert "buttons" not in vps.notifies[-1] and "Иванов" in vps.notifies[-1]["text"]


def test_agent_alerts(env):
    sync, vps = env
    run(sync.sync_once())
    sync.hub.disconnected_at = time.time() - 100
    run(sync._check_alerts())
    run(sync._check_alerts())
    assert sum("Агент ворот не на связи" in n["text"] for n in vps.notifies) == 1
    sync.hub.ws = object()
    run(sync._check_alerts())
    assert "снова на связи" in vps.notifies[-1]["text"]


class FakeHttp:
    def __init__(self):
        self.calls = []

    async def get(self, url, params=None, **kw):
        self.calls.append(("GET", url, params))

    async def post(self, url, content=None, **kw):
        self.calls.append(("POST", url, content))


def test_kuma_ping(env):
    sync, _ = env
    sync.http = FakeHttp()
    sync.cfg.healthcheck.url = "http://203.0.113.10:3001/api/push/AbC123?status=up&msg=OK&ping="
    run(sync._ping_healthcheck())
    method, url, params = sync.http.calls[-1]
    assert method == "GET" and url == "http://203.0.113.10:3001/api/push/AbC123"
    assert params["status"] == "down" and "агент" in params["msg"]
    sync.hub.ws = object()
    run(sync._ping_healthcheck())
    assert sync.http.calls[-1][2]["status"] == "up"
