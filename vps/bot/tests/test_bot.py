"""Бот на VPS: имитация портала Битрикс24 (httpx.MockTransport) и обмена с объектом."""

import asyncio
import json
import time
from datetime import datetime, timezone

import httpx
import pytest

from app.bitrix import Bitrix24Bot
from app.config import BotConfig
from app.site import SiteState

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
        self.unregistered = []

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
            return httpx.Response(200, json={"result": {"commands": [
                {"id": i, "command": "/" + c} for i, c in enumerate(self.commands)]}})
        if method == "imbot.v2.Command.unregister":
            self.unregistered.append(p["commandId"])
            return httpx.Response(200, json={"result": True})
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

    def add_command(self, cmd, params="", user=ADMIN, age=0, context="keyboard"):
        date = datetime.fromtimestamp(time.time() - age, timezone.utc).isoformat()
        self.events.append({
            "eventId": 1000 + len(self.events), "type": "ONIMBOTV2COMMANDADD", "date": date,
            "data": {"command": {"command": "/" + cmd, "params": params, "context": context},
                     "chat": {"dialogId": "chat42"}, "user": {"id": user, "name": "Аня", "bot": False}},
        })


@pytest.fixture()
def env(tmp_path):
    portal = FakePortal()
    cfg = BotConfig(webhook_url="https://p.bitrix24.ru/rest/1/x/", bot_token="tok", user_ids=[ADMIN],
                    site_token="s", data_dir=tmp_path, poll_interval=0.05)
    site = SiteState(tmp_path)
    bot = Bitrix24Bot(cfg, site)
    bot.client = httpx.AsyncClient(transport=httpx.MockTransport(portal.handler))
    return bot, site, portal


def run(coro):
    return asyncio.run(coro)


async def poll_once(bot):
    task = asyncio.create_task(bot.poll_loop())
    await asyncio.sleep(0.3)
    task.cancel()


def site_sync(bot, results=(), **extra):
    payload = {"site": "Ворота", "status": "🟢 Агент ворот: на связи", "last_events": "12:00 А123ВС 77 · открыто",
               "plates": [{"plate": "А123ВС 77", "owner": "Иванов", "active": True, "valid_until": None}],
               "results": list(results), **extra}
    return run(bot.on_site_sync(payload))


def test_setup_without_open_command(env):
    bot, _, portal = env
    portal.commands = ["open"]  # осталась от старой версии
    run(bot.setup())
    assert bot.ready and bot.dialog_id == "chat42"
    assert portal.unregistered == [0]
    assert set(portal.commands) >= {"status", "last", "list", "add", "del", "help"}
    assert all(b["COMMAND"] != "open" for b in __import__("app.bitrix", fromlist=["MENU"]).MENU)


def test_add_queued_and_result_delivered(env):
    bot, site, portal = env
    site_sync(bot)  # объект на связи
    portal.add_command("add", "А123ВС77 Иванов", context="textarea")
    run(poll_once(bot))
    assert "Передаю на объект" in portal.sent[-1][1]
    ops = site_sync(bot)
    assert ops == [{"id": ops[0]["id"], "cmd": "add", "args": "А123ВС77 Иванов", "who": "Аня"}]
    # объект применил правку и прислал результат
    ops2 = site_sync(bot, results=[{"id": ops[0]["id"], "text": "✅ А123ВС 77 добавлен (Иванов) — Аня"}])
    assert ops2 == [] and portal.sent[-1] == ("chat42", "✅ А123ВС 77 добавлен (Иванов) — Аня", portal.sent[-1][2])


def test_add_while_site_offline(env):
    bot, site, portal = env
    site_sync(bot)
    site.data["last_sync"] -= 600
    portal.add_command("del", "А123ВС77")
    run(poll_once(bot))
    assert "Нет связи с объектом" in portal.sent[-1][1] and "будет выполнена" in portal.sent[-1][1]
    assert site.ops[0]["cmd"] == "del"


def test_status_list_last(env):
    bot, site, portal = env
    site_sync(bot)
    for c in ("status", "list", "last"):
        portal.add_command(c)
    run(poll_once(bot))
    status, plates, last = (m for _, m, _ in portal.sent[-3:])
    assert "Объект на связи" in status and "Агент ворот: на связи" in status
    assert "А123ВС 77 — Иванов" in plates
    assert "А123ВС 77 · открыто" in last


def test_status_offline_shows_last_data(env):
    bot, site, portal = env
    site_sync(bot)
    site.data["last_sync"] -= 600
    portal.add_command("status")
    run(poll_once(bot))
    text = portal.sent[-1][1]
    assert "Нет связи с объектом" in text and "Последние данные от объекта" in text


def test_open_command_not_supported(env):
    bot, site, portal = env
    site_sync(bot)
    portal.add_command("open")
    run(poll_once(bot))
    assert "Из Битрикс24 ворота не открываются" in portal.sent[-1][1]
    assert site.ops == []


def test_foreign_user(env):
    bot, site, portal = env
    portal.add_command("add", "А123ВС77", user=99)
    run(poll_once(bot))
    assert site.ops == [] and "Ваш ID в Битрикс24: 99" in portal.sent[-1][1]


def test_notify_with_photo_and_add_button(env):
    bot, _, portal = env
    run(bot.setup())
    run(bot.on_site_notify({"text": "⛔ Неизвестный номер", "photo": {"name": "s.jpg", "b64": "AAAA"},
                            "buttons": [{"text": "➕ В список", "command": "add", "params": "K555MM99"},
                                        {"text": "Открыть", "command": "open"}]}))
    assert portal.uploads[-1] == ("chat42", "s.jpg", "⛔ Неизвестный номер", "AAAA")
    upd = [p for m, p in portal.calls if m == "imbot.v2.Chat.Message.update"][-1]
    assert [b["COMMAND"] for b in upd["fields"]["keyboard"]] == ["add"]  # «открыть» отброшено


def test_offline_alert_and_recovery(env):
    bot, site, portal = env
    run(bot.setup())
    site_sync(bot)
    site.data["last_sync"] -= 300
    bot.cfg.site_offline_alert_after = 60

    async def watch():
        task = asyncio.create_task(bot.watch_site())
        await asyncio.sleep(0.1)
        task.cancel()

    run(watch())
    assert "Нет связи с объектом" in portal.sent[-1][1]
    site_sync(bot)
    assert "Связь с объектом восстановлена" in portal.sent[-1][1] and "5 мин" in portal.sent[-1][1]


def test_expired_op(env):
    bot, site, portal = env
    run(bot.setup())
    op = site.add_op("add", "А123ВС77", "Аня", "chat42")
    op["ts"] -= 8 * 86400
    ops = site_sync(bot)
    assert ops == [] and "не выходил на связь" in portal.sent[-1][1]


# --- реальные события портала: пустые объекты приходят как [] ---------------------------
def raw_event(data, kind="ONIMBOTV2COMMANDADD"):
    return {"eventId": 1, "type": kind, "date": "2026-09-24T23:50:24+03:00", "data": data}


def test_event_with_empty_chat_list_private_dialog(env):
    bot, site, portal = env
    run(bot.setup())
    ev = raw_event({"command": {"command": "/status", "params": ""}, "chat": [],
                    "user": {"id": ADMIN, "name": "Аня", "bot": False}, "message": {"id": 5, "text": "/status"}})
    run(bot.handle(ev))
    dialog, text, _ = portal.sent[-1]
    assert dialog == str(ADMIN) and "ни разу не выходил на связь" in text


def test_event_with_empty_chat_list_group_chat(env):
    bot, site, portal = env
    run(bot.setup())  # чат «Ворота» — chat42
    ev = raw_event({"command": {"command": "/list"}, "chat": [],
                    "user": {"id": ADMIN, "name": "Аня"}, "message": {"id": 5, "chatId": 42, "text": "/list"}})
    run(bot.handle(ev))
    assert portal.sent[-1][0] == "chat42"


def test_event_with_empty_command_uses_message_text(env):
    bot, site, portal = env
    run(bot.setup())
    ev = raw_event({"command": [], "chat": {"dialogId": "chat42"}, "user": {"id": ADMIN},
                    "message": {"text": "/add А123ВС77 Иванов"}})
    run(bot.handle(ev))
    assert site.ops[-1]["cmd"] == "add" and site.ops[-1]["args"] == "А123ВС77 Иванов"


def test_event_with_list_data_is_ignored(env):
    bot, _, portal = env
    run(bot.setup())
    n = len(portal.sent)
    run(bot.handle(raw_event([])))
    run(bot.handle(raw_event({"chat": [], "user": [], "message": []})))
    assert len(portal.sent) == n  # не падает и никому не отвечает
