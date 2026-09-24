import json
import os
import tempfile
import threading
from pathlib import Path

import pytest

_tmp = tempfile.mkdtemp()
Path(_tmp, "config.yaml").write_text("cameras: []\n", encoding="utf-8")
os.environ["LPR_CONFIG"] = str(Path(_tmp, "config.yaml"))
os.environ["LPR_DATA_DIR"] = str(Path(_tmp, "data"))

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(main.app) as c:
        yield c


def login(c):
    r = c.post("/login", data={"username": "admin", "password": "test-pass"}, follow_redirects=False)
    assert r.status_code == 303


def test_login_required(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/api/status").status_code == 401
    r = client.post("/login", data={"username": "admin", "password": "bad"})
    assert r.status_code == 401


def test_plates_crud(client):
    login(client)
    client.post("/plates", data={"plate": "а 123 вс 77", "owner": "Иванов"})
    plates = client.get("/api/plates").json()
    assert plates[0]["plate"] == "A123BC77" and plates[0]["owner"] == "Иванов"
    page = client.get("/plates").text
    assert "А123ВС 77" in page
    pid = plates[0]["id"]
    client.post(f"/plates/{pid}/toggle")
    assert main.db.allowed_plates() == {}
    client.post(f"/plates/{pid}/delete")
    assert client.get("/api/plates").json() == []


def test_open_without_agent(client):
    r = client.post("/api/open", headers={"Authorization": "Bearer api-token"})
    assert r.status_code == 503 and not r.json()["ok"]


def test_agent_rejects_bad_token(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/agent", headers={"Authorization": "Bearer nope"}) as ws:
            ws.receive_text()


def test_open_via_agent(client):
    with client.websocket_connect("/ws/agent", headers={"Authorization": "Bearer agent-token"}) as ws:
        ws.send_text(json.dumps({"type": "hello", "driver": "dummy"}))
        result = {}

        def call():
            result["r"] = client.post("/api/open", headers={"Authorization": "Bearer api-token"})

        t = threading.Thread(target=call)
        t.start()
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "open"
        ws.send_text(json.dumps({"type": "ack", "id": msg["id"], "ok": True}))
        t.join(5)
        assert result["r"].json()["ok"] is True
        status = client.get("/api/status", headers={"Authorization": "Bearer api-token"}).json()
        assert status["agent"]["online"] and status["agent"]["info"]["driver"] == "dummy"


def test_pages_render(client):
    login(client)
    main.db.add_event(camera="outside", plate="A123BC77", decision="denied", detail="нет в списке")
    for url in ("/", "/events", "/events?decision=denied", "/plates?add=A123BC77", "/partials/events"):
        r = client.get(url)
        assert r.status_code == 200, url
    assert "А123ВС 77" in client.get("/events").text
    assert client.get("/plates/export.csv").status_code == 200
    r = client.post("/plates/import", files={"file": ("p.csv", "plate;owner\nК555ММ99;Петров\n".encode())},
                    follow_redirects=False)
    assert r.status_code == 303
    assert any(p["plate"] == "K555MM99" for p in main.db.list_plates())
    assert client.get("/media/../../etc/passwd").status_code == 404
