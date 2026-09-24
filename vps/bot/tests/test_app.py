"""Протокол /link через настоящий WebSocket (бот Битрикс24 отключён)."""

import functools
import json
import threading

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import main


@pytest.fixture()
def client():
    with TestClient(main.app) as c:
        yield c


def test_bad_token_rejected(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/link", headers={"Authorization": "Bearer wrong"}) as ws:
            ws.receive_text()


def test_link_request_roundtrip(client):
    assert client.get("/healthz").json()["site"]["online"] is False
    with client.websocket_connect("/link", headers={"Authorization": "Bearer link-secret"}) as ws:
        ws.send_text(json.dumps({"type": "hello", "site": "Ворота"}))
        ws.send_text(json.dumps({"type": "status", "text": "всё хорошо"}))
        result = {}

        def ask():
            result["text"] = client.portal.call(functools.partial(main.link.request, "status", params="", who="Аня"))

        t = threading.Thread(target=ask)
        t.start()
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "command" and msg["cmd"] == "status" and msg["who"] == "Аня"
        ws.send_text(json.dumps({"type": "result", "id": msg["id"], "text": "📡 всё хорошо"}))
        t.join(5)
        assert result["text"] == "📡 всё хорошо"
        health = client.get("/healthz").json()
        assert health["site"]["online"] and health["site"]["info"]["site"] == "Ворота"
        assert main.link.last_status[1] == "всё хорошо"
