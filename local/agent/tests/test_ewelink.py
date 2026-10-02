"""Клиент облака eWeLink и драйвер агента против имитации облака (локальный HTTP-сервер)."""

import json
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import ewelink_cloud as ec
import ewelink_setup
import gate_agent

APPID, SECRET = "TESTAPPID", "TESTSECRET"


class Cloud:
    """Состояние имитации: токены, устройство и журнал запросов."""

    def __init__(self):
        self.at, self.rt = "AT1", "RT1"
        self.expired = False          # access token «истёк»: любой вызов с ним получает 402
        self.params = {"switch": "off", "pulse": "on", "pulseWidth": 1000}
        self.device_error = 0
        self.log = []                 # (method, path, headers, body)
        self.http_status = 200


def make_handler(cloud: Cloud):
    class H(BaseHTTPRequestHandler):
        def _send(self, obj, status=None):
            raw = json.dumps(obj).encode()
            self.send_response(status or cloud.http_status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(raw)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def _record(self, body):
            cloud.log.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body))

        def _signed_ok(self, body):
            return (self.headers.get("X-CK-Appid") == APPID
                    and self.headers.get("Authorization") == "Sign " + ec.make_sign(SECRET, body))

        def _bearer_error(self):
            if self.headers.get("Authorization") != "Bearer " + cloud.at:
                return 401
            if cloud.expired:
                return 402
            return 0

        def do_POST(self):
            body = self._body()
            self._record(body)
            path = urllib.parse.urlparse(self.path).path
            if path == "/v2/user/oauth/token":
                req = json.loads(body)
                if not self._signed_ok(body):
                    return self._send({"error": 400, "msg": "sign verification failed", "data": {}})
                if req["code"] != "good-code":
                    return self._send({"error": 405, "msg": "invalid code", "data": {}})
                now = int(time.time() * 1000)
                return self._send({"error": 0, "msg": "", "data": {
                    "accessToken": cloud.at, "refreshToken": cloud.rt,
                    "atExpiredTime": now + 30 * 86400000, "rtExpiredTime": now + 60 * 86400000}})
            if path == "/v2/user/refresh":
                req = json.loads(body)
                if not self._signed_ok(body) or req["rt"] != cloud.rt:
                    return self._send({"error": 401, "msg": "bad refresh token", "data": {}})
                cloud.at, cloud.rt, cloud.expired = cloud.at + "x", cloud.rt + "x", False
                return self._send({"error": 0, "msg": "", "data": {"at": cloud.at, "rt": cloud.rt}})
            if path == "/v2/device/thing/status":
                err = self._bearer_error()
                if err:
                    return self._send({"error": err, "msg": "token", "data": {}})
                if cloud.device_error:
                    return self._send({"error": cloud.device_error, "msg": "device", "data": {}})
                req = json.loads(body)
                cloud.params.update(req["params"])
                return self._send({"error": 0, "msg": "", "data": {}})
            self._send({"error": 403, "msg": "api not found", "data": {}})

        def do_GET(self):
            self._record(b"")
            u = urllib.parse.urlparse(self.path)
            err = self._bearer_error()
            if err:
                return self._send({"error": err, "msg": "token", "data": {}})
            if u.path == "/v2/device/thing":
                item = {"name": "Ворота", "deviceid": "1000abc", "online": True, "params": dict(cloud.params),
                        "productModel": "SV", "extra": {"uiid": 14}}
                return self._send({"error": 0, "msg": "", "data": {"thingList": [
                    {"itemType": 1, "itemData": item}, {"itemType": 3, "itemData": {"name": "группа"}}]}})
            if u.path == "/v2/device/thing/status":
                q = urllib.parse.parse_qs(u.query)
                want = q["params"][0].split("|")
                return self._send({"error": 0, "msg": "", "data": {
                    "params": {k: v for k, v in cloud.params.items() if k in want}}})
            self._send({"error": 403, "msg": "api not found", "data": {}})

        def log_message(self, *a):
            pass

    return H


@pytest.fixture()
def cloud(tmp_path):
    state = Cloud()
    server = HTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state.base = f"http://127.0.0.1:{server.server_port}"
    state.token_file = str(tmp_path / "token.json")
    state.client = ec.EweLinkClient(APPID, SECRET, state.token_file, api_base=state.base, timeout=3)
    yield state
    server.shutdown()


def login(cloud):
    return cloud.client.exchange_code("good-code", "http://127.0.0.1:8888/redirectUrl", "eu")


# --- подпись по официальным примерам документации ----------------------------------------
def test_sign_matches_official_examples():
    assert ec.make_sign("abc", "ABC_123") == "v1+mfNY2ukxswM8sZOTg99srZsVnUVv9DGXeav1096M="
    body = ec.compact_json({"email": "1234@gmail.com", "password": "12345678", "countryCode": "+1"})
    assert ec.make_sign("OdPuCZ4PkPPi0rVKRVcGmll2NM6vVk0c", body) == "ttZ/gluzqrafvGonjMD20p4//arW6KoZKbo1SOMEzCA="


def test_authorization_url(cloud):
    url = cloud.client.authorization_url("http://127.0.0.1:8888/redirectUrl", "st8", seq=123, nonce="zt123456")
    u = urllib.parse.urlparse(url)
    q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
    assert u.netloc == "c2ccdn.coolkit.cc" and u.path == "/oauth/index.html"
    assert q["clientId"] == APPID and q["seq"] == "123" and q["state"] == "st8"
    assert q["redirectUrl"] == "http://127.0.0.1:8888/redirectUrl" and q["grantType"] == "authorization_code"
    assert q["authorization"] == ec.make_sign(SECRET, f"{APPID}_123")


# --- вход и токены -----------------------------------------------------------------------------
def test_exchange_code_signs_body_and_saves_tokens(cloud):
    tok = login(cloud)
    method, path, headers, body = cloud.log[-1]
    assert (method, path) == ("POST", "/v2/user/oauth/token")
    assert headers["authorization"] == "Sign " + ec.make_sign(SECRET, body)
    assert headers["x-ck-appid"] == APPID and len(headers["x-ck-nonce"]) == 8
    saved = json.load(open(cloud.token_file, encoding="utf-8"))
    assert saved["accessToken"] == "AT1" and saved["refreshToken"] == "RT1" and saved["region"] == "eu"
    assert tok == saved | {"updated": tok["updated"]}


def test_bad_code_is_explained(cloud):
    with pytest.raises(ec.EweLinkError) as e:
        cloud.client.exchange_code("bad", "http://127.0.0.1:8888/redirectUrl", "eu")
    assert e.value.code == 405 and "30 секунд" in str(e.value)


def test_missing_token_file_hint(cloud):
    with pytest.raises(ec.EweLinkError) as e:
        cloud.client.set_switch("1000abc")
    assert "ewelink_setup.py login" in str(e.value)


# --- команда реле -------------------------------------------------------------------------------
def test_set_switch_request(cloud):
    login(cloud)
    cloud.client.set_switch("1000abc", "on")
    method, path, headers, body = cloud.log[-1]
    assert (method, path) == ("POST", "/v2/device/thing/status")
    assert headers["authorization"] == "Bearer AT1"
    assert json.loads(body) == {"type": 1, "id": "1000abc", "params": {"switch": "on"}}
    assert cloud.params["switch"] == "on"


def test_expired_token_is_refreshed_and_command_retried(cloud):
    login(cloud)
    cloud.expired = True
    cloud.client.set_switch("1000abc", "on")
    paths = [p for _, p, _, _ in cloud.log]
    assert paths[-3:] == ["/v2/device/thing/status", "/v2/user/refresh", "/v2/device/thing/status"]
    assert json.load(open(cloud.token_file, encoding="utf-8"))["accessToken"] == "AT1x"
    assert cloud.params["switch"] == "on"


def test_token_close_to_expiry_is_refreshed_before_use(cloud):
    login(cloud)
    tok = json.load(open(cloud.token_file, encoding="utf-8"))
    tok["atExpiredTime"] = int(time.time() * 1000) + 3600_000  # через час
    json.dump(tok, open(cloud.token_file, "w", encoding="utf-8"))
    cloud.client.set_switch("1000abc", "on")
    assert [p for _, p, _, _ in cloud.log][-2:] == ["/v2/user/refresh", "/v2/device/thing/status"]


def test_failed_refresh_asks_to_login_again(cloud):
    login(cloud)
    cloud.expired = True
    cloud.rt = "other"  # refresh token на сервере уже другой (вход был с другого места)
    with pytest.raises(ec.EweLinkError) as e:
        cloud.client.set_switch("1000abc", "on")
    assert e.value.code == 401 and "login" in str(e.value)


def test_refresh_if_older(cloud):
    login(cloud)
    assert cloud.client.refresh_if_older(7 * 86400) is False
    tok = json.load(open(cloud.token_file, encoding="utf-8"))
    tok["updated"] = time.time() - 8 * 86400
    json.dump(tok, open(cloud.token_file, "w", encoding="utf-8"))
    assert cloud.client.refresh_if_older(7 * 86400) is True
    assert json.load(open(cloud.token_file, encoding="utf-8"))["refreshToken"] == "RT1x"


def test_device_errors_have_readable_text(cloud):
    login(cloud)
    cloud.device_error = 4002
    with pytest.raises(ec.EweLinkError) as e:
        cloud.client.set_switch("1000abc", "on")
    assert "в сети Wi-Fi" in str(e.value)
    cloud.device_error = 407
    with pytest.raises(ec.EweLinkError) as e:
        cloud.client.set_switch("1000abc", "on")
    assert "APPID" in str(e.value)


def test_non_json_403_is_quota_hint(cloud):
    login(cloud)
    cloud.http_status = 403

    class Boom(Exception):
        pass

    # ответ имитации остаётся JSON; проверяем разбор HTTP-ошибки без JSON отдельно
    import urllib.error
    import io
    err = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b"<html>forbidden</html>"))
    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(err)
    try:
        with pytest.raises(ec.EweLinkError) as e:
            cloud.client.set_switch("1000abc", "on")
    finally:
        urllib.request.urlopen = orig
    assert e.value.code == 403 and "лимит" in str(e.value)


def test_no_internet_message():
    c = ec.EweLinkClient(APPID, SECRET, "x.json", api_base="http://127.0.0.1:9", timeout=1)
    with pytest.raises(ec.EweLinkError) as e:
        c._request("GET", "http://127.0.0.1:9/x", {})
    assert "нет связи" in str(e.value)


# --- драйвер агента ------------------------------------------------------------------------------
def make_driver(cloud):
    return gate_agent.make_driver({"driver": "ewelink", "ewelink": {
        "appid": APPID, "appsecret": SECRET, "device_id": "1000abc",
        "token_file": cloud.token_file, "api_base": cloud.base, "timeout": 3}})


def test_driver_pulse_sends_switch_on(cloud):
    login(cloud)
    drv = make_driver(cloud)
    drv.maintain()
    drv.pulse(1.0)
    assert cloud.params["switch"] == "on" and "приняло команду" in drv.last_response


def test_driver_refuses_when_inching_is_off(cloud):
    login(cloud)
    cloud.params["pulse"] = "off"
    drv = make_driver(cloud)
    drv.maintain()
    assert "Inching" in drv.unsafe
    with pytest.raises(RuntimeError):
        drv.pulse(1.0)
    assert cloud.params["switch"] == "off"           # реле не включалось


def test_driver_refuses_too_long_inching_and_recovers_when_fixed(cloud):
    login(cloud)
    cloud.params["pulseWidth"] = 5000
    drv = make_driver(cloud)
    drv.maintain()
    assert "2,5 с" in drv.unsafe
    with pytest.raises(RuntimeError):
        drv.pulse(1.0)
    cloud.params["pulseWidth"] = 1000                 # пользователь исправил настройку
    drv.pulse(1.0)                                    # блокировка снимается сразу
    assert drv.unsafe == "" and cloud.params["switch"] == "on"


def test_driver_missing_setting_is_explained():
    with pytest.raises(KeyError):
        gate_agent.make_driver({"driver": "ewelink", "ewelink": {"appid": "x"}})
    assert "appsecret" in gate_agent.explain_error(KeyError("appsecret"))


def test_explain_error_for_ewelink():
    assert "реле не в сети" in gate_agent.explain_error(ec.EweLinkError(30022))


# --- помощники ewelink_setup ------------------------------------------------------------
def test_parse_redirect():
    got = ewelink_setup.parse_redirect("http://127.0.0.1:8888/redirectUrl?code=abc&region=eu&state=S1")
    assert got == {"code": "abc", "region": "eu", "state": "S1"}


def test_local_redirect_receiver():
    result = {}

    def run():
        result.update(ewelink_setup.wait_for_code("127.0.0.1", 18765, "S1", timeout=10))

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.5)
    # чужое состояние (state) не принимается, правильное принимается
    for q, ok in (("code=bad&region=eu&state=OTHER", False), ("code=good&region=as&state=S1", True)):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:18765/redirectUrl?{q}", timeout=3).read()
            assert ok
        except urllib.error.HTTPError as e:
            assert not ok and e.code == 404
    t.join(10)
    assert result == {"code": "good", "region": "as", "state": "S1"}
