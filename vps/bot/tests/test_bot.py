"""Бот на VPS: имитация портала Битрикс24 (httpx.MockTransport) и объекта (SiteLink)."""

import asyncio
import json
import time
from datetime import datetime, timezone

import httpx
import pytest

from app.bitrix import Bitrix24Bot
from app.config import BotConfig
from app.link import SiteLink

ADMIN = 7


class FakePortal:
    def __init__(self):
        self.bot = None
        self.commands = []
        self.events = []
        self.sent = []
        self.uploads = []
        self.chats = []
        self.calls = []

    def handler(self, request):
        method = request.url.path.rsplit("/", 1)[-1]
        p = json.loads(request.content or b"{}")
        self.calls.append((method, p))
        if method == "imbot.v2.Bot.get":
            if not self.bot:
                return httpx.Response(400, json={"error": "BOT_NOT_FOUND"})
            return httpx.Response(200, json={"result": {"bot": self.bot}})
        if method == "imbot.v2.Bot.register":
            assert p["fields"]["eventMode"] == "fetch"
            self.bot = {"id": 500}
            return httpx.Response(200, json={"result": {"bot": self.bot}})
        assert p.get("botToken") == "tok" and p.get("botId") == 500, method
        if method == "imbot.v2.Command.list":
            return httpx.Response(200, json={"result": {"commands": [{"command": "/" + c} for c in self.commands]}})
        if method == "imbot.v2.Command.register":
            self.commands.append(p["fields"]["command"])
            return httpx.Response(200, json={"result": {}})
        if method == "imbot.v2.Chat.add":
            self.chats.append(p["fields"])
            return httpx.Response(200, json={"result": {"chat": {"dialogId": "chat42"}}})
        if method == "imbot.v2.Event.get":
            evs = [e for e in self.events if e["eventId"] >= p.get("offset", 0)]
            nxt = max([e["eventId"] for e in evs], default=p.get("offset", 0) - 1) + 1
            return httpx.Response(200, json={"result": {"events": evs, "nextOffset": nxt, "hasMore": False}})
        if method == "imbot.v2.Chat.Message.send":
            self.sent.append((p["dialogId"], p["fields"].get("message"), p["fields"].get("keyboard")))
            return httpx.Response(200, json={"result": {"id": 1}})
        if method == "imbot.v2.Chat.Message.update":
            return httpx.Response(200, json={"result": True})
        if method == "imbot.v2.File.upload":
            self.uploads.append((p["dialogId"], p["fields"]["name"], p["fields"]["message"], p["fields"]["content"]))
            return httpx.Response(200, json={"result": {"messageId": 99}})
        return httpx.Response(400, json={"error": "METHOD_NOT_FOUND"})

    def add_command(self, cmd, params="", user=ADMIN, age=0):
        date = datetime.fromtimestamp(time.time() - age, timezone.utc).isoformat()
        self.events.append({
            "eventId": 1000 + len(self.events), "type": "ONIMBOTV2COMMANDADD", "date": date,
            "data": {"command": {"command": "/" + cmd, "params": params, "context": "keyboard"},
                     "chat": {"dialogId": "chat42"}, "user": {"id": user, "name": "Аня", "bot": False}},
        })


class FakeSiteWS:
    """Объект, который отвечает на команды: «ok:<команда>:<параметры>»."""

    def __init__(self, link: SiteLink, silent=False):
        self.link, self.silent, self.commands = link, silent, []
        self.client = "test"

    async def send_text(self, raw):
        msg = json.loads(raw)
        self.commands.append(msg)
        if not self.silent:
            fut = self.link._pending[msg["id"]]
            fut.set_result({"text": f"ok:{msg['cmd']}:{msg.get('params', '')}:{msg.get('who', '')}"})


@pytest.fixture()
def env(tmp_path):
    portal = FakePortal()
    cfg = BotConfig(webhook_url="https://p.bitrix24.ru/rest/1/x/", bot_token="tok", user_ids=[ADMIN],
                    link_token="s", data_dir=tmp_path, poll_interval=0.05, command_timeout=0.5)
    link = SiteLink()
    bot = Bitrix24Bot(cfg, link)
    bot.client = httpx.AsyncClient(transport=httpx.MockTransport(portal.handler))
    return bot, link, portal


def run(coro):
    return asyncio.run(coro)


async def poll_once(bot):
    task = asyncio.create_task(bot.poll_loop())
    await asyncio.sleep(0.3)
    task.cancel()


def test_setup(env):
    bot, _, portal = env
    run(bot.setup())
    assert bot.ready and bot.dialog_id == "chat42"
    assert set(portal.commands) == {"open", "status", "last", "list", "add", "del", "help"}


def test_command_forwarded_to_site(env):
    bot, link, portal = env
    link.ws = FakeSiteWS(link)
    portal.add_command("add", "А123ВС77 Иванов")
    run(poll_once(bot))
    assert portal.sent[-1][1] == "ok:add:А123ВС77 Иванов:Аня"
    assert bot.state["offset"] == 1001


def test_site_offline(env):
    bot, link, portal = env
    link.disconnected_at = time.time() - 600
    portal.add_command("open")
    portal.add_command("status")
    link.last_status = (time.time() - 610, "🟢 Агент ворот: на связи")
    run(poll_once(bot))
    open_reply, status_reply = portal.sent[-2][1], portal.sent[-1][1]
    assert "Нет связи с объектом" in open_reply and "не выполнена" in open_reply
    assert "Бот на VPS: работает" in status_reply and "Агент ворот: на связи" in status_reply


def test_site_timeout(env):
    bot, link, portal = env
    link.ws = FakeSiteWS(link, silent=True)
    portal.add_command("list")
    run(poll_once(bot))  # команда ждёт ответа 0.5 с, опрос отменяется раньше
    run(bot.handle(portal.events[0]))
    assert "не ответил" in portal.sent[-1][1]


def test_stale_open_not_forwarded(env):
    bot, link, portal = env
    link.ws = FakeSiteWS(link)
    portal.add_command("open", age=600)
    run(poll_once(bot))
    assert link.ws.commands == [] and "не выполнена" in portal.sent[-1][1]


def test_foreign_user(env):
    bot, link, portal = env
    link.ws = FakeSiteWS(link)
    portal.add_command("open", user=99)
    run(poll_once(bot))
    assert link.ws.commands == [] and "Ваш ID в Битрикс24: 99" in portal.sent[-1][1]


def test_help_answered_locally(env):
    bot, link, portal = env
    portal.add_command("help")
    run(poll_once(bot))
    assert "/add А123ВС77" in portal.sent[-1][1]


def test_notify_with_photo_and_buttons(env):
    bot, _, portal = env
    run(bot.setup())
    run(bot.on_site_notify({"text": "⛔ Неизвестный номер", "photo": {"name": "s.jpg", "b64": "AAAA"},
                            "buttons": [{"text": "Открыть", "command": "open", "params": "ev:5"}]}))
    assert portal.uploads[-1] == ("chat42", "s.jpg", "⛔ Неизвестный номер", "AAAA")
    upd = [p for m, p in portal.calls if m == "imbot.v2.Chat.Message.update"][-1]
    assert upd["fields"]["keyboard"][0]["COMMAND_PARAMS"] == "ev:5"


def test_offline_alert_and_recovery_report(env):
    bot, link, portal = env
    run(bot.setup())
    bot.cfg.site_offline_alert_after = 1
    link.disconnected_at = time.time() - 300

    async def scenario():
        task = asyncio.create_task(bot.watch_site())
        await asyncio.sleep(0.1)
        task.cancel()
        link.ws = FakeSiteWS(link)
        await bot.on_site_connect(link.disconnected_at)

    run(scenario())
    texts = [m for _, m, _ in portal.sent]
    assert any("Нет связи с объектом" in t for t in texts)
    assert "Связь с объектом восстановлена" in texts[-1] and "5 мин" in texts[-1]
    assert "ok:outage_summary" in texts[-1]
