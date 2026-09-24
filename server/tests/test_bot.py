import asyncio
import tempfile
import time
from pathlib import Path

import pytest

from app.bot import BTN_OPEN, BTN_STATUS, TelegramBot
from app.config import Config, GateConfig, RecognitionConfig, TelegramConfig
from app.db import Database
from app.gate import AgentHub

CHAT = 111


class FakeController:
    def __init__(self):
        self.opens = 0

    async def open_gate(self, reason, force=False):
        self.opens += 1
        return True, "открыто"


@pytest.fixture()
def bot():
    tmp = Path(tempfile.mkdtemp())
    cfg = Config(cameras=[], recognition=RecognitionConfig(), gate=GateConfig(),
                 telegram=TelegramConfig(token="x", chat_ids=[CHAT]), data_dir=tmp)
    b = TelegramBot(cfg, Database(tmp / "t.db"), AgentHub(), {}, {})
    b.controller = FakeController()
    b.sent = []

    async def fake_call(method, **kw):
        b.sent.append((method, kw.get("data", {})))
        return {"ok": True, "result": []}

    b._call = fake_call
    return b


def texts(b):
    return [d.get("text") or d.get("caption") for m, d in b.sent if m in ("sendMessage", "sendPhoto")]


def msg(text, age=0, chat=CHAT):
    return {"message": {"chat": {"id": chat}, "from": {"first_name": "Аня"}, "text": text,
                        "date": int(time.time() - age)}}


def run(coro):
    return asyncio.run(coro)


def test_open_button(bot):
    run(bot._handle(msg(BTN_OPEN)))
    assert bot.controller.opens == 1
    assert "открываются" in texts(bot)[-1]


def test_stale_open_is_refused(bot):
    run(bot._handle(msg("/open", age=600)))
    assert bot.controller.opens == 0
    assert "не выполнена" in texts(bot)[-1]


def test_foreign_chat_is_refused(bot):
    run(bot._handle(msg("/open", chat=999)))
    assert bot.controller.opens == 0
    assert "Доступ запрещён" in texts(bot)[-1]


def test_add_list_del(bot):
    run(bot._handle(msg("/add а123вс77 Иван Петров до 31.12.2030")))
    p = bot.db.find_plate("A123BC77")
    assert p["owner"] == "Иван Петров" and p["valid_until"] == "2030-12-31"
    run(bot._handle(msg("/list")))
    assert "А123ВС 77 — Иван Петров" in texts(bot)[-1]
    run(bot._handle(msg("/del А123ВС77")))
    assert bot.db.find_plate("A123BC77") is None


def test_status_shows_agent_offline(bot):
    run(bot._handle(msg(BTN_STATUS)))
    t = texts(bot)[-1]
    assert "Сервер: работает" in t and "Агент ворот: НЕ на связи" in t


def test_outage_recovery_report_and_stale_callbacks(bot):
    bot.offline_since = time.time() - 600
    bot.db.add_event(camera="c", plate="A123BC77", decision="granted", ts=time.time() - 300)
    run(bot._mark_online())
    report = texts(bot)[-1]
    assert "восстановлена" in report and "10 мин" in report and "открыто по номеру: 1" in report
    assert bot.last_outage is not None
    # нажатие кнопки, накопившееся за время обрыва, не выполняется
    cb = {"id": "1", "from": {"first_name": "Аня"}, "data": "open:1",
          "message": {"chat": {"id": CHAT}, "message_id": 5, "date": int(time.time() - 60)}}
    run(bot._handle({"callback_query": cb}))
    assert bot.controller.opens == 0
    bot._stale_batch = False
    run(bot._handle({"callback_query": cb}))
    assert bot.controller.opens == 1


def test_short_outage_is_ignored(bot):
    bot.offline_since = time.time() - 10
    run(bot._mark_online())
    assert texts(bot) == [] and not bot._stale_batch


def test_agent_alerts(bot):
    bot.hub.disconnected_at = time.time() - 100
    run(bot._check_alerts())
    run(bot._check_alerts())
    alerts = [t for t in texts(bot) if "Агент ворот не на связи" in t]
    assert len(alerts) == 1
    bot.hub.ws = object()  # агент подключился
    run(bot._check_alerts())
    assert "снова на связи" in texts(bot)[-1]


def test_no_sends_while_offline(bot):
    bot.offline_since = time.time()
    run(bot.broadcast("test"))
    assert texts(bot) == []
