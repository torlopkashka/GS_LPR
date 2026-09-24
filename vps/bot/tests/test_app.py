"""HTTP-обмен ПК у ворот с VPS (бот Битрикс24 отключён — нет вебхука)."""

import pytest
from fastapi.testclient import TestClient

from app import main

AUTH = {"Authorization": "Bearer site-secret"}


@pytest.fixture()
def client():
    with TestClient(main.app) as c:
        yield c


def test_bad_token(client):
    assert client.post("/api/site/sync", json={}, headers={"Authorization": "Bearer x"}).status_code == 401
    assert client.post("/api/site/notify", json={"text": "x"}).status_code == 401


def test_sync_roundtrip(client):
    assert client.get("/healthz").json()["site"]["online"] is False
    op = main.site.add_op("add", "А123ВС77 Иванов", "Аня", "chat42")
    r = client.post("/api/site/sync", headers=AUTH, json={
        "site": "Ворота", "status": "ok", "plates": [], "last_events": "", "results": []})
    assert r.status_code == 200 and r.json()["ops"][0]["id"] == op["id"]
    r = client.post("/api/site/sync", headers=AUTH, json={
        "site": "Ворота", "status": "ok", "plates": [{"plate": "А123ВС 77"}], "last_events": "",
        "results": [{"id": op["id"], "text": "✅ добавлен"}]})
    assert r.json()["ops"] == []
    health = client.get("/healthz").json()
    assert health["site"]["online"] and health["site"]["pending_ops"] == 0
    assert main.site.data["plates"] == [{"plate": "А123ВС 77"}]


def test_notify_accepted(client):
    r = client.post("/api/site/notify", headers=AUTH, json={"text": "⛔ Неизвестный номер"})
    assert r.json() == {"ok": True}
