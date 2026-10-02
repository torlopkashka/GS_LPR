"""Локальный режим eWeLink: шифрование, протокол и драйвер против имитации реле."""

import base64
import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import ewelink_lan as lan
import gate_agent
from test_ewelink import APPID, SECRET, cloud, login  # noqa: F401  (фикстура cloud)

DEVICE, KEY = "1000abc123", "11111111-2222-3333-4444-555555555555"


class Relay:
    def __init__(self):
        self.params = {"switch": "off", "pulse": "on", "pulseWidth": 1000}
        self.log = []
        self.fail = False


@pytest.fixture()
def relay():
    state = Relay()

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            cmd = self.path.rsplit("/", 1)[-1]
            if state.fail:
                self.connection.close()
                return
            data = json.loads(lan.aes_cbc_decrypt(lan._key(KEY), base64.b64decode(body["iv"]),
                                                  base64.b64decode(body["data"])))
            state.log.append((cmd, body["deviceid"], data))
            if cmd == "switch":
                state.params["switch"] = data["switch"]
            iv = os.urandom(16)
            reply = {"seq": 1, "error": 0, "iv": base64.b64encode(iv).decode(),
                     "data": base64.b64encode(lan.aes_cbc_encrypt(
                         lan._key(KEY), iv, json.dumps(state.params).encode())).decode()}
            raw = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state.host = f"127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()


def lan_driver(tmp_path, host, cloud=None):
    lan_file = tmp_path / "lan.json"
    lan.save_state(str(lan_file), {"deviceid": DEVICE, "devicekey": KEY, "host": host})
    ew = {"device_id": DEVICE, "lan_file": str(lan_file), "timeout": 3}
    if cloud:
        ew.update(appid=APPID, appsecret=SECRET, token_file=cloud.token_file, api_base=cloud.base)
    return gate_agent.make_driver({"driver": "ewelink", "ewelink": ew})


def test_aes_known_vector():
    # FIPS-197, приложение C.1
    rk = lan._expand_key(bytes.fromhex("000102030405060708090a0b0c0d0e0f"))
    c = lan._encrypt_block(bytes.fromhex("00112233445566778899aabbccddeeff"), rk)
    assert c.hex() == "69c4e0d86a7b0430d8cdb78070b4c55a"
    assert lan._decrypt_block(c, rk).hex() == "00112233445566778899aabbccddeeff"


@pytest.mark.skipif(not shutil.which("openssl"), reason="нужен openssl для сверки")
def test_aes_cbc_matches_openssl():
    for n in (0, 7, 16, 33):
        key, iv, data = os.urandom(16), os.urandom(16), os.urandom(n)
        ref = subprocess.run(["openssl", "enc", "-aes-128-cbc", "-K", key.hex(), "-iv", iv.hex()],
                             input=data, capture_output=True, check=True).stdout
        assert lan.aes_cbc_encrypt(key, iv, data) == ref
        assert lan.aes_cbc_decrypt(key, iv, ref) == data


def test_wrong_key_is_detected():
    iv = os.urandom(16)
    ct = lan.aes_cbc_encrypt(lan._key("a"), iv, b'{"switch":"on"}')
    with pytest.raises(ValueError):
        lan.aes_cbc_decrypt(lan._key("b"), iv, ct)


def test_switch_on_request(relay):
    lan.switch_on(relay.host, DEVICE, KEY)
    assert relay.log == [("switch", DEVICE, {"switch": "on"})]


def test_probe_checks_key(relay):
    assert lan.probe(relay.host, DEVICE, KEY)["pulseWidth"] == 1000
    assert lan.probe(relay.host, DEVICE, "other-key") is None


def test_connection_failure_is_explained():
    with pytest.raises(lan.LanError) as e:
        lan.switch_on("127.0.0.1:9", DEVICE, KEY, timeout=0.5)
    assert "нет связи" in str(e.value)


def test_driver_uses_lan_without_cloud(relay, tmp_path):
    drv = lan_driver(tmp_path, relay.host)           # облако не настроено вовсе
    drv.maintain()
    drv.pulse(1.0)
    assert relay.params["switch"] == "on" and "по локальной сети" in drv.last_response


def test_driver_lan_does_not_touch_cloud(relay, tmp_path, cloud):
    login(cloud)
    drv = lan_driver(tmp_path, relay.host, cloud)
    before = len(cloud.log)
    drv.maintain()
    drv.pulse(1.0)
    assert relay.params["switch"] == "on"
    assert len(cloud.log) == before                  # облако не использовалось


def test_driver_falls_back_to_cloud(relay, tmp_path, cloud, monkeypatch):
    login(cloud)
    relay.fail = True
    drv = lan_driver(tmp_path, relay.host, cloud)
    monkeypatch.setattr(lan, "discover", lambda *a, **k: None)
    drv.pulse(1.0)
    assert cloud.params["switch"] == "on" and "облако" in drv.last_response


def test_driver_without_cloud_reports_lan_failure(relay, tmp_path, monkeypatch):
    relay.fail = True
    drv = lan_driver(tmp_path, relay.host)
    monkeypatch.setattr(lan, "discover", lambda *a, **k: None)
    with pytest.raises(lan.LanError):
        drv.pulse(1.0)


def test_driver_rediscovers_moved_relay_and_saves_host(relay, tmp_path, monkeypatch):
    drv = lan_driver(tmp_path, "127.0.0.1:9")        # старый адрес не отвечает
    monkeypatch.setattr(lan, "discover", lambda *a, **k: relay.host)
    drv.pulse(1.0)
    assert relay.params["switch"] == "on"
    assert lan.load_state(drv.lan_file)["host"] == relay.host


def test_driver_blocks_on_bad_inching_via_lan(relay, tmp_path):
    relay.params["pulse"] = "off"
    drv = lan_driver(tmp_path, relay.host)
    drv.maintain()
    assert "Inching" in drv.unsafe
    with pytest.raises(RuntimeError):
        drv.pulse(1.0)
    assert relay.params["switch"] == "off"


def test_driver_needs_some_transport(tmp_path):
    with pytest.raises(RuntimeError) as e:
        gate_agent.make_driver({"driver": "ewelink", "ewelink": {
            "device_id": DEVICE, "lan_file": str(tmp_path / "none.json")}})
    assert "локальный режим" in str(e.value)
