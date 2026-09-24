"""Бот Битрикс24 против имитации REST-портала (httpx.MockTransport)."""

import asyncio
import json
import time
from datetime import datetime, timezone

import httpx
import pytest

from app.b24bot import B24Error, Bitrix24Bot
from app.config import Bitrix24Config, Config, GateConfig, RecognitionConfig
from app.db import Database
from app.gate import AgentHub

WEBHOOK = "https://test.bitrix24.ru/rest/1/secret/"
ADMIN = 7


class FakePortal:
    def __init__(self):
        self.bot = None
        self.commands = []
        self.events = []
        self.sent = []      # (dialogId, message, keyboard)
        self.uploads = []
        self.chats = []
        self.calls = []
        self.fail_network = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail_network:
            raise httpx.ConnectError("no internet")
        method = request.url.path.rsplit("/", 1)[-1]
        p = json.loads(request.content or b"{}")
        self.calls.append((method, p))
        if method != "imbot.v2.Bot.register":
            assert p.get("botToken") == "tok", method
        if method == "imbot.v2.Bot.get":
            if not self.bot:
                return httpx.Response(400, json={"error": "BOT_NOT_FOUND", "error_description": "Bot not found"})
            return httpx.Response(200, json={"result": {"bot": self.bot}})
        if method == "imbot.v2.Bot.register":
            f = p["fields"]
            assert f["eventMode"] == "fetch" and f["botToken"] == "tok"
            self.bot = {"id": 500, "code": f["code"]}
            return httpx.Response(200, json={"result": {"bot": self.bot}})
        assert p.get("botId") == 500, method
        if method == "imbot.v2.Command.list":
            return httpx.Response(200, json={"result": {"commands": [{"command": "/" + c} for c in self.commands]}})
        if method == "imbot.v2.Command.register":
            self.commands.append(p["fields"]["command"])
            return httpx.Response(200, json={"result": {"command": {"id": len(self.commands)}}})
        if method == "imbot.v2.Chat.add":
            self.chats.append(p["fields"])
            return httpx.Response(200, json={"result": {"chat": {"id": 42, "dialogId": "chat42"}}})
        if method == "imbot.v2.Event.get":
            evs = [e for e in self.events if e["eventId"] >= p.get("offset", 0)]
            nxt = max([e["eventId"] for e in evs], default=p.get("offset", 0) - 1) + 1
            return httpx.Response(200, json={"result": {"events": evs, "nextOffset": nxt, "hasMore": False}})
        if method == "imbot.v2.Chat.Message.send":
            f = p["fields"]
            self.sent.append((p["dialogId"], f.get("message"), f.get("keyboard")))
            return httpx.Response(200, json={"result": {"id": len(self.sent)}})
        if method == "imbot.v2.Chat.User.list":
            return httpx.Response(200, json={"result": [{"id": u} for u in self.chats[-1]["userIds"]]})
        if method == "imbot.v2.Chat.User.add":
            self.chats[-1]["userIds"] += p["userIds"]
            return httpx.Response(200, json={"result": True})
        if method == "imbot.v2.Chat.Message.update":
            return httpx.Response(200, json={"result": True})
        if method == "imbot.v2.File.upload":
            self.uploads.append((p["dialogId"], p["fields"]["name"], p["fields"]["message"]))
            return httpx.Response(200, json={"result": {"messageId": 99}})
        return httpx.Response(400, json={"error": "METHOD_NOT_FOUND"})

    def add_command(self, cmd, params="", user=ADMIN, age=0, context="keyboard"):
        date = datetime.fromtimestamp(time.time() - age, timezone.utc).isoformat()
        self.events.append({
            "eventId": 1000 + len(self.events), "type": "ONIMBOTV2COMMANDADD", "date": date,
            "data": {"command": {"command": "/" + cmd, "params": params, "context": context},
                     "chat": {"dialogId": "chat42"}, "user": {"id": user, "name": "Аня", "bot": False},
                     "message": {"id": 1, "text": f"/{cmd} {params}"}},
        })


class FakeController:
    def __init__(self):
        self.opens = 0

    async def open_gate(self, reason, force=False):
        self.opens += 1
        return True, "открыто"


@pytest.fixture()
def env(tmp_path):
    portal = FakePortal()
    cfg = Config(cameras=[], recognition=RecognitionConfig(), gate=GateConfig(), data_dir=tmp_path,
                 bitrix24=Bitrix24Config(webhook_url=WEBHOOK, bot_token="tok", user_ids=[ADMIN]))
    bot = Bitrix24Bot(cfg, Database(tmp_path / "t.db"), AgentHub(), {}, {})
    bot.client = httpx.AsyncClient(transport=httpx.MockTransport(portal.handler))
    bot.controller = FakeController()
    return bot, portal


def run(coro):
    return asyncio.run(coro)


async def poll_once(bot):
    """Одна итерация цикла опроса событий."""
    task = asyncio.create_task(bot.poll_loop())
    await asyncio.sleep(0.2)
    task.cancel()


def test_setup_registers_bot_commands_and_chat(env):
    bot, portal = env
    run(bot.setup())
    assert bot.bot_id == 500 and bot.ready
    assert set(portal.commands) == {"open", "status", "last", "list", "add", "del", "help"}
    assert portal.chats[0]["userIds"] == [ADMIN]
    assert bot.dialog_id == "chat42"
    # повторный запуск: бот и команды уже есть, чат сохранён в data/bitrix24.json
    bot2 = Bitrix24Bot(bot.cfg, bot.db, bot.hub, {}, {})
    bot2.client = bot.client
    run(bot2.setup())
    assert len(portal.commands) == 7 and len(portal.chats) == 1 and bot2.dialog_id == "chat42"
    # новый сотрудник в B24_USER_IDS добавляется в чат при следующем запуске
    bot2.bx.user_ids = [ADMIN, 8]
    bot3 = Bitrix24Bot(bot.cfg, bot.db, bot.hub, {}, {})
    bot3.client = bot.client
    run(bot3.setup())
    assert portal.chats[0]["userIds"] == [ADMIN, 8]


def test_open_button(env):
    bot, portal = env
    run(bot.setup())
    portal.add_command("open")
    bot.bx.poll_interval = 0.05
    run(poll_once(bot))
    assert bot.controller.opens == 1
    assert "Ворота открываются" in portal.sent[-1][1]
    assert bot.state["offset"] == 1001  # событие подтверждено и больше не придёт


def test_stale_open_refused(env):
    bot, portal = env
    run(bot.setup())
    portal.add_command("open", age=600)
    run(poll_once(bot))
    assert bot.controller.opens == 0
    assert "не выполнена" in portal.sent[-1][1]


def test_foreign_user_refused(env):
    bot, portal = env
    run(bot.setup())
    portal.add_command("open", user=99)
    run(poll_once(bot))
    assert bot.controller.opens == 0
    assert "Ваш ID в Битрикс24: 99" in portal.sent[-1][1]


def test_add_list_del(env):
    bot, portal = env
    run(bot.setup())
    portal.add_command("add", "а123вс77 Иван Петров до 31.12.2030", context="textarea")
    portal.add_command("list")
    run(poll_once(bot))
    p = bot.db.find_plate("A123BC77")
    assert p["owner"] == "Иван Петров" and p["valid_until"] == "2030-12-31"
    assert "А123ВС 77 — Иван Петров" in portal.sent[-1][1]
    portal.add_command("del", "А123ВС77")
    run(poll_once(bot))
    assert bot.db.find_plate("A123BC77") is None


def test_notification_with_photo_and_buttons(env, tmp_path):
    bot, portal = env
    run(bot.setup())
    snap = tmp_path / "s.jpg"
    snap.write_bytes(b"\xff\xd8jpeg")
    ev_id = bot.db.add_event(camera="c", plate="K555MM99", decision="denied")
    run(bot.event(bot.db.get_event(ev_id), snap))
    dialog, name, msg = portal.uploads[-1]
    assert dialog == "chat42" and "Неизвестный номер: К555ММ 99" in msg
    upd = [p for m, p in portal.calls if m == "imbot.v2.Chat.Message.update"][-1]
    cmds = [(b["COMMAND"], b.get("COMMAND_PARAMS")) for b in upd["fields"]["keyboard"]]
    assert cmds == [("open", f"ev:{ev_id}"), ("add", f"ev:{ev_id}")]
    # нажатие «В список» под уведомлением
    portal.add_command("add", f"ev:{ev_id}")
    run(poll_once(bot))
    assert bot.db.find_plate("K555MM99") is not None


def test_old_notification_button_refused(env):
    bot, portal = env
    run(bot.setup())
    ev_id = bot.db.add_event(camera="c", plate="K555MM99", decision="denied", ts=time.time() - 3600)
    portal.add_command("open", f"ev:{ev_id}")
    run(poll_once(bot))
    assert bot.controller.opens == 0 and "устарело" in portal.sent[-1][1]


def test_outage_report(env):
    bot, portal = env
    run(bot.setup())
    bot.offline_since = time.time() - 600
    bot.db.add_event(camera="c", plate="A123BC77", decision="granted", ts=time.time() - 300)
    run(bot._mark_online())
    report = portal.sent[-1][1]
    assert "восстановлена" in report and "10 мин" in report and "открыто по номеру: 1" in report


def test_network_failure_marks_offline_and_skips_sends(env):
    bot, portal = env
    run(bot.setup())
    portal.fail_network = True
    run(poll_once(bot))
    assert bot.offline_since is not None
    n = len(portal.sent)
    run(bot.broadcast("test"))
    assert len(portal.sent) == n


def test_status_and_agent_alerts(env):
    bot, portal = env
    run(bot.setup())
    portal.add_command("status")
    run(poll_once(bot))
    assert "Агент ворот: НЕ на связи" in portal.sent[-1][1]
    bot.hub.disconnected_at = time.time() - 100
    run(bot._check_alerts())
    run(bot._check_alerts())
    assert sum("Агент ворот не на связи" in (m or "") for _, m, _ in portal.sent) == 1
    bot.hub.ws = object()
    run(bot._check_alerts())
    assert "снова на связи" in portal.sent[-1][1]


def test_api_error_is_raised(env):
    bot, portal = env
    run(bot.setup())
    with pytest.raises(B24Error):
        run(bot._call("imbot.v2.Unknown"))


class FakeHttp:
    def __init__(self):
        self.calls = []

    async def get(self, url, params=None, **kw):
        self.calls.append(("GET", url, params))

    async def post(self, url, content=None, **kw):
        self.calls.append(("POST", url, content))


def test_healthcheck_formats(env):
    bot, _ = env
    bot.http = FakeHttp()
    bot.cfg.healthcheck.url = "https://mon.example.ru/api/push/AbC123?status=up&msg=OK&ping="
    run(bot._ping_healthcheck())
    method, url, params = bot.http.calls[-1]
    assert method == "GET" and url == "https://mon.example.ru/api/push/AbC123"
    assert params["status"] == "down" and "агент" in params["msg"]
    bot.hub.ws = object()
    run(bot._ping_healthcheck())
    assert bot.http.calls[-1][2]["status"] == "up"
    bot.cfg.healthcheck.url = "https://hc-ping.com/uuid"
    bot.hub.ws = None
    run(bot._ping_healthcheck())
    assert bot.http.calls[-1][1] == "https://hc-ping.com/uuid/fail"
