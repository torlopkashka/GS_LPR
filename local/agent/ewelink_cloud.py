"""Клиент облачного API eWeLink (API v2, вход по OAuth 2.0).

Только стандартная библиотека Python. Протокол: официальная документация
https://github.com/coolkit-technologies/ewelink-api (OAuth2.0.md, UIIDProtocol.md).

Нужны собственные APPID и APP SECRET с портала разработчика https://dev.ewelink.cc
(для частных лиц бесплатно, поддерживается только вход по OAuth 2.0). Чужие ключи
приложений (например, из Home Assistant или мобильного приложения) использовать нельзя.

Схема работы:
  1. один раз `ewelink_setup.py login`: пользователь входит в свой аккаунт eWeLink в браузере,
     клиент обменивает код на токены и сохраняет их в файл;
  2. агент перед командой читает токены, при необходимости обновляет их (access token живёт
     30 дней, refresh token 60 дней) и отправляет команду POST /v2/device/thing/status.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import socket
import string
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HOSTS = {
    "cn": "https://cn-apia.coolkit.cn",
    "as": "https://as-apia.coolkit.cc",
    "us": "https://us-apia.coolkit.cc",
    "eu": "https://eu-apia.coolkit.cc",
}
AUTH_PAGE = "https://c2ccdn.coolkit.cc/oauth/index.html"
DAY = 86400

HINTS = {
    400: "неверные параметры запроса",
    401: "токен недействителен (например, был вход с другого места). Выполните вход заново: "
         "python ewelink_setup.py login",
    402: "срок действия токена истёк. Выполните вход заново: python ewelink_setup.py login",
    403: "облако отклонило запрос (адрес не найден или превышен месячный лимит бесплатного ключа)",
    405: "ресурс не найден (неверный device_id или код авторизации просрочен: он действует 30 секунд)",
    406: "у аккаунта нет прав на это устройство",
    407: "у вашего APPID нет прав на эту операцию или на этот тип устройства (ограничение бесплатного ключа)",
    412: "превышен лимит запросов бесплатного ключа (50 000 в месяц)",
    500: "внутренняя ошибка облака eWeLink, повторите позже",
    4002: "облако не смогло передать команду реле: проверьте, что реле включено и в сети Wi-Fi",
    30022: "реле не в сети (нет питания или Wi-Fi)",
}


class EweLinkError(Exception):
    def __init__(self, code: int, msg: str = ""):
        self.code = code
        self.msg = msg
        hint = HINTS.get(code, "")
        text = f"eWeLink, ошибка {code}" if code else "eWeLink"
        if hint:
            text += f": {hint}"
        if msg and msg not in text:
            text += f" ({msg})"
        super().__init__(text)


def make_sign(secret: str, message: bytes | str) -> str:
    """HMAC-SHA256 с ключом APP SECRET, результат в Base64."""
    if isinstance(message, str):
        message = message.encode()
    return base64.b64encode(hmac.new(secret.encode(), message, hashlib.sha256).digest()).decode()


def make_nonce() -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=8))


def compact_json(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()


class EweLinkClient:
    def __init__(self, appid: str, appsecret: str, token_file: str = "ewelink_token.json",
                 api_base: str | None = None, timeout: float = 8.0):
        self.appid = appid
        self.appsecret = appsecret
        self.token_file = token_file
        self.api_base = api_base.rstrip("/") if api_base else None
        self.timeout = timeout
        self._lock = threading.RLock()

    # --- HTTP ----------------------------------------------------------------------------
    def _base(self, region: str) -> str:
        if self.api_base:
            return self.api_base
        if region not in HOSTS:
            raise EweLinkError(0, f"неизвестный регион {region!r}, ожидается cn, as, us или eu")
        return HOSTS[region]

    def _request(self, method: str, url: str, headers: dict, body: bytes | None = None) -> dict:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return json.loads(raw)
            except ValueError:
                raise EweLinkError(e.code, f"HTTP {e.code} {e.reason}") from None
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            reason = getattr(e, "reason", e)
            raise EweLinkError(0, f"нет связи с облаком eWeLink ({reason}). Проверьте интернет на этом ПК") from None
        try:
            return json.loads(raw)
        except ValueError:
            raise EweLinkError(0, "облако вернуло ответ неизвестного формата") from None

    @staticmethod
    def _check(resp: dict) -> dict:
        if resp.get("error", 0) != 0:
            raise EweLinkError(int(resp["error"]), str(resp.get("msg", "")))
        return resp.get("data") or {}

    def _signed_post(self, region: str, path: str, payload: dict) -> dict:
        """Запросы до входа (обмен кода, обновление токена): подпись тела ключом APP SECRET."""
        body = compact_json(payload)
        headers = {
            "Content-Type": "application/json",
            "X-CK-Appid": self.appid,
            "X-CK-Nonce": make_nonce(),
            "Authorization": "Sign " + make_sign(self.appsecret, body),
        }
        return self._check(self._request("POST", self._base(region) + path, headers, body))

    # --- токены --------------------------------------------------------------------------
    def load_tokens(self) -> dict | None:
        try:
            with open(self.token_file, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def save_tokens(self, tok: dict) -> None:
        d = os.path.dirname(os.path.abspath(self.token_file))
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".ewelink_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(tok, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.token_file)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def authorization_url(self, redirect_url: str, state: str, seq: int | None = None,
                          nonce: str | None = None) -> str:
        seq = seq if seq is not None else int(time.time() * 1000)
        query = {
            "state": state,
            "clientId": self.appid,
            "authorization": make_sign(self.appsecret, f"{self.appid}_{seq}"),
            "seq": str(seq),
            "redirectUrl": redirect_url,
            "nonce": nonce or make_nonce(),
            "grantType": "authorization_code",
            "showQRCode": "false",
        }
        return AUTH_PAGE + "?" + urllib.parse.urlencode(query)

    def exchange_code(self, code: str, redirect_url: str, region: str) -> dict:
        data = self._signed_post(region, "/v2/user/oauth/token", {
            "code": code, "redirectUrl": redirect_url, "grantType": "authorization_code"})
        now = int(time.time() * 1000)
        tok = {
            "region": region,
            "accessToken": data["accessToken"],
            "refreshToken": data["refreshToken"],
            "atExpiredTime": int(data.get("atExpiredTime") or now + 30 * DAY * 1000),
            "rtExpiredTime": int(data.get("rtExpiredTime") or now + 60 * DAY * 1000),
            "updated": time.time(),
        }
        self.save_tokens(tok)
        return tok

    def refresh(self) -> dict:
        with self._lock:
            tok = self.load_tokens()
            if not tok:
                raise EweLinkError(0, "нет файла с токенами: выполните вход командой "
                                      "python ewelink_setup.py login")
            data = self._signed_post(tok["region"], "/v2/user/refresh", {"rt": tok["refreshToken"]})
            now = int(time.time() * 1000)
            tok.update(accessToken=data["at"], refreshToken=data["rt"],
                       atExpiredTime=now + 30 * DAY * 1000, rtExpiredTime=now + 60 * DAY * 1000,
                       updated=time.time())
            self.save_tokens(tok)
            return tok

    def refresh_if_older(self, seconds: float) -> bool:
        """Плановое обновление: токены, которым больше seconds, заменяются новыми."""
        with self._lock:
            tok = self.load_tokens()
            if tok and time.time() - tok.get("updated", 0) >= seconds:
                self.refresh()
                return True
            return False

    def _fresh_tokens(self) -> dict:
        with self._lock:
            tok = self.load_tokens()
            if not tok:
                raise EweLinkError(0, "нет файла с токенами: выполните вход командой "
                                      "python ewelink_setup.py login")
            if tok["atExpiredTime"] - time.time() * 1000 < 2 * DAY * 1000:
                try:
                    tok = self.refresh()
                except EweLinkError:
                    if tok["atExpiredTime"] <= time.time() * 1000:
                        raise
            return tok

    # --- вызовы после входа -----------------------------------------------------------
    def _authed(self, method: str, path: str, payload: dict | None = None, query: dict | None = None) -> dict:
        for attempt in (0, 1):
            tok = self._fresh_tokens()
            url = self._base(tok["region"]) + path
            if query:
                url += "?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
            headers = {
                "Authorization": "Bearer " + tok["accessToken"],
                "X-CK-Nonce": make_nonce(),
                "Content-Type": "application/json",
            }
            body = compact_json(payload) if payload is not None else None
            resp = self._request(method, url, headers, body)
            if resp.get("error") in (401, 402) and attempt == 0:
                self.refresh()  # токен устарел: обновить и повторить один раз
                continue
            return self._check(resp)
        raise EweLinkError(401)  # недостижимо, для полноты

    def devices(self) -> list[dict]:
        data = self._authed("GET", "/v2/device/thing", query={"num": 0})
        return [i["itemData"] for i in data.get("thingList", []) if "deviceid" in i.get("itemData", {})]

    def status(self, device_id: str, params: tuple[str, ...] = ("switch", "pulse", "pulseWidth")) -> dict:
        data = self._authed("GET", "/v2/device/thing/status",
                            query={"type": 1, "id": device_id, "params": "|".join(params)})
        return data.get("params", {})

    def set_params(self, device_id: str, params: dict) -> None:
        self._authed("POST", "/v2/device/thing/status", {"type": 1, "id": device_id, "params": params})

    def set_switch(self, device_id: str, state: str = "on") -> None:
        self.set_params(device_id, {"switch": state})
