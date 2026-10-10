"""Управление реле eWeLink по локальной сети (без облака и интернета).

Только стандартная библиотека Python. Протокол неофициальный, описан по открытому
проекту SonoffLAN (https://github.com/AlexxIT/SonoffLAN, файл core/ewelink/local.py):

  * реле слушает HTTP на порту 8081 своего IP-адреса;
  * команда:  POST http://IP:8081/zeroconf/<команда>, например /zeroconf/switch;
  * тело — JSON {sequence, deviceid, selfApikey, data, encrypt, iv}, где data — это JSON команды,
    зашифрованный AES-128-CBC (PKCS7) и записанный в Base64; ключ — MD5 от devicekey реле,
    iv случайный (16 байт, тоже в Base64);
  * ответ {"error": 0, ...}; у зашифрованных устройств data в ответе зашифровано так же.

devicekey — ключ конкретного реле, его отдаёт облачный API (`ewelink_setup.py lan` сохраняет
его в файл один раз). Дальше облако для команд не нужно.

Адрес реле ищется запросом mDNS (_ewelink._tcp) или, если он не помог, перебором порта 8081
в подсети ПК с проверкой, что устройство отвечает именно с этим ключом.
"""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import os
import socket
import struct
import time
import urllib.error
import urllib.request

PORT = 8081
MDNS_GROUP, MDNS_PORT = "224.0.0.251", 5353


# --------------------------------------------------------------------------
# AES-128-CBC на чистом Python (нужен только для коротких сообщений, скорость не важна)
# --------------------------------------------------------------------------
def _make_sbox() -> list[int]:
    # Таблица подстановки AES по определению: обратный элемент в GF(2^8) и аффинное преобразование
    p = q = 1
    sbox = [0] * 256
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)      # p *= 3
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09                                              # q /= 3
        x = q ^ ((q << 1) | (q >> 7)) & 0xFF ^ ((q << 2) | (q >> 6)) & 0xFF \
            ^ ((q << 3) | (q >> 5)) & 0xFF ^ ((q << 4) | (q >> 4)) & 0xFF
        sbox[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


SBOX = _make_sbox()
INV_SBOX = [0] * 256
for _i, _v in enumerate(SBOX):
    INV_SBOX[_v] = _i


def _xt(a: int) -> int:
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else a << 1


def _mul(a: int, b: int) -> int:
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xt(a)
        b >>= 1
    return r


def _expand_key(key: bytes) -> list[list[int]]:
    w = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    rcon = 1
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [SBOX[b] for b in t]
            t[0] ^= rcon
            rcon = _xt(rcon)
        w.append([a ^ b for a, b in zip(w[i - 4], t)])
    return [sum(w[r * 4:r * 4 + 4], []) for r in range(11)]      # 11 раундовых ключей по 16 байт


def _encrypt_block(block: bytes, rk: list[list[int]]) -> bytes:
    s = [b ^ k for b, k in zip(block, rk[0])]
    for r in range(1, 11):
        s = [SBOX[b] for b in s]
        # ShiftRows (состояние хранится по столбцам)
        s = [s[(i + 4 * (i % 4)) % 16] for i in range(16)]
        if r != 10:
            m = []
            for c in range(0, 16, 4):
                a0, a1, a2, a3 = s[c:c + 4]
                m += [_xt(a0) ^ (_xt(a1) ^ a1) ^ a2 ^ a3,
                      a0 ^ _xt(a1) ^ (_xt(a2) ^ a2) ^ a3,
                      a0 ^ a1 ^ _xt(a2) ^ (_xt(a3) ^ a3),
                      (_xt(a0) ^ a0) ^ a1 ^ a2 ^ _xt(a3)]
            s = m
        s = [b ^ k for b, k in zip(s, rk[r])]
    return bytes(s)


def _decrypt_block(block: bytes, rk: list[list[int]]) -> bytes:
    s = [b ^ k for b, k in zip(block, rk[10])]
    for r in range(9, -1, -1):
        # InvShiftRows
        s = [s[(i - 4 * (i % 4)) % 16] for i in range(16)]
        s = [INV_SBOX[b] for b in s]
        s = [b ^ k for b, k in zip(s, rk[r])]
        if r != 0:
            m = []
            for c in range(0, 16, 4):
                a0, a1, a2, a3 = s[c:c + 4]
                m += [_mul(a0, 14) ^ _mul(a1, 11) ^ _mul(a2, 13) ^ _mul(a3, 9),
                      _mul(a0, 9) ^ _mul(a1, 14) ^ _mul(a2, 11) ^ _mul(a3, 13),
                      _mul(a0, 13) ^ _mul(a1, 9) ^ _mul(a2, 14) ^ _mul(a3, 11),
                      _mul(a0, 11) ^ _mul(a1, 13) ^ _mul(a2, 9) ^ _mul(a3, 14)]
            s = m
    return bytes(s)


def aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    rk = _expand_key(key)
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    out, prev = b"", iv
    for i in range(0, len(data), 16):
        prev = _encrypt_block(bytes(a ^ b for a, b in zip(data[i:i + 16], prev)), rk)
        out += prev
    return out


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    if not data or len(data) % 16:
        raise ValueError("длина зашифрованных данных не кратна 16")
    rk = _expand_key(key)
    out, prev = b"", iv
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        out += bytes(a ^ b for a, b in zip(_decrypt_block(block, rk), prev))
        prev = block
    pad = out[-1]
    if not 1 <= pad <= 16:
        raise ValueError("неверное дополнение: вероятно, неверный devicekey")
    return out[:-pad]


# --------------------------------------------------------------------------
# Протокол реле
# --------------------------------------------------------------------------
class LanError(Exception):
    pass


def _key(devicekey: str) -> bytes:
    return hashlib.md5(devicekey.encode()).digest()


def encrypt_payload(deviceid: str, devicekey: str, data: dict, iv: bytes | None = None) -> dict:
    iv = iv or os.urandom(16)
    plain = json.dumps(data, separators=(",", ":")).encode()
    return {
        "sequence": str(int(time.time() * 1000)),
        "deviceid": deviceid,
        "selfApikey": "123",
        "encrypt": True,
        "iv": base64.b64encode(iv).decode(),
        "data": base64.b64encode(aes_cbc_encrypt(_key(devicekey), iv, plain)).decode(),
    }


def decrypt_response(resp: dict, devicekey: str) -> dict:
    if not resp.get("data") or not resp.get("iv"):
        return {}
    raw = aes_cbc_decrypt(_key(devicekey), base64.b64decode(resp["iv"]), base64.b64decode(resp["data"]))
    return json.loads(raw)


def send(host: str, deviceid: str, devicekey: str, command: str, data: dict | None = None,
         timeout: float = 3.0, retries: int = 3) -> dict:
    """Отправляет команду реле и возвращает расшифрованные данные ответа.

    Веб-сервер реле обрабатывает один запрос за раз и может сбросить соединение, поэтому
    при сбое запрос повторяется.
    """
    if ":" not in host:
        host += f":{PORT}"
    last: Exception | None = None
    for attempt in range(retries):
        body = json.dumps(encrypt_payload(deviceid, devicekey, data or {})).encode()
        req = urllib.request.Request(f"http://{host}/zeroconf/{command}", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "Connection": "close"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
        except (urllib.error.URLError, OSError) as e:
            last = LanError(f"нет связи с реле {host} по локальной сети: {getattr(e, 'reason', e)}")
            time.sleep(0.2)
            continue
        try:
            resp = json.loads(raw)
        except ValueError:
            raise LanError("реле ответило не JSON: возможно, по этому адресу не реле eWeLink") from None
        if resp.get("error", 0) != 0:
            raise LanError(f"реле отклонило команду {command}: ошибка {resp.get('error')}")
        try:
            return decrypt_response(resp, devicekey)
        except (ValueError, KeyError) as e:
            raise LanError(f"не удалось расшифровать ответ реле ({e}): проверьте devicekey") from None
    raise last or LanError("нет ответа от реле")


def switch_on(host: str, deviceid: str, devicekey: str, timeout: float = 3.0) -> None:
    send(host, deviceid, devicekey, "switch", {"switch": "on"}, timeout)


def probe(host: str, deviceid: str, devicekey: str, timeout: float = 1.5) -> dict | None:
    """Проверяет, что по адресу отвечает именно это реле (ответ расшифровывается его ключом).

    Используется запрос getState из проекта SonoffLAN: он ничего не включает. Запрос info
    относится к режиму DIY и на заводской прошивке не работает."""
    try:
        return send(host, deviceid, devicekey, "getState", {}, timeout, retries=1)
    except (LanError, OSError):
        return None


def diagnose(host: str, deviceid: str, devicekey: str, timeout: float = 3.0) -> tuple[str, str]:
    """Подробная проверка адреса для команды lan. Возвращает (уровень, пояснение):
    verified — реле ответило и ответ расшифрован ключом; open — порт открыт, но подтверждения
    ключом нет (на заводской прошивке getState может не отвечать); closed — порт недоступен."""
    hostport = host if ":" in host else f"{host}:{PORT}"
    name, port = hostport.rsplit(":", 1)
    s = socket.socket()
    s.settimeout(timeout)
    try:
        if s.connect_ex((name, int(port))) != 0:
            return "closed", f"порт {port} на {name} не отвечает"
    finally:
        s.close()
    body = json.dumps(encrypt_payload(deviceid, devicekey, {})).encode()
    req = urllib.request.Request(f"http://{hostport}/zeroconf/getState", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Connection": "close"})
    raw, status, ctype = b"", None, ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status, ctype = r.read(500), r.status, r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        raw, status, ctype = e.read(500), e.code, e.headers.get("Content-Type", "")
    except (urllib.error.URLError, OSError) as e:
        return "open", f"порт открыт, но на запрос getState реле не ответило ({getattr(e, 'reason', e)})"
    text = raw.decode("utf-8", "replace").strip().replace("\n", " ")[:200]
    try:
        resp = json.loads(raw)
        if resp.get("error", 1) == 0:
            try:
                decrypt_response(resp, devicekey)
                return "verified", "реле ответило, ответ расшифрован ключом"
            except (ValueError, KeyError):
                return "open", f"реле ответило, но расшифровать ответ ключом не удалось: {text}"
        return "open", f"реле ответило ошибкой на getState: {text}"
    except ValueError:
        return "open", f"реле ответило не JSON (HTTP {status}, {ctype or 'без типа'}): {text or 'пустой ответ'}"


# --------------------------------------------------------------------------
# Поиск реле в сети
# --------------------------------------------------------------------------
def local_ip() -> str | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))     # пакеты не отправляются, нужен только выбор интерфейса
        return s.getsockname()[0]
    except OSError:
        pass
    finally:
        s.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                return ip
    except OSError:
        pass
    return None


def _mdns_query() -> bytes:
    name = b"".join(bytes([len(p)]) + p.encode() for p in ("_ewelink", "_tcp", "local")) + b"\x00"
    return struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + name + struct.pack(">HH", 12, 1)   # PTR, IN


def discover_mdns(deviceid: str, wait: float = 3.0) -> str | None:
    """Запрос mDNS: реле отвечает прямо на наш адрес, имя службы содержит его device_id."""
    needle = deviceid.lower().encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.4)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        deadline = time.time() + wait
        next_query = 0.0
        while time.time() < deadline:
            if time.time() >= next_query:
                try:
                    s.sendto(_mdns_query(), (MDNS_GROUP, MDNS_PORT))
                except OSError:
                    return None
                next_query = time.time() + 1.0
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                continue
            if needle in data.lower():
                return addr[0]
    finally:
        s.close()
    return None


def discover_scan(deviceid: str, devicekey: str, ip: str | None = None) -> str | None:
    """Перебор адресов /24 вокруг ПК: на порту 8081 спрашиваем info и проверяем расшифровку ключом."""
    ip = ip or local_ip()
    if not ip:
        return None
    prefix = ip.rsplit(".", 1)[0]
    hosts = [f"{prefix}.{i}" for i in range(1, 255)]

    def check(h: str) -> str | None:
        s = socket.socket()
        s.settimeout(0.4)
        try:
            if s.connect_ex((h, PORT)) != 0:
                return None
        finally:
            s.close()
        return h if probe(h, deviceid, devicekey) is not None else None

    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        for found in pool.map(check, hosts):
            if found:
                return found
    return None


def discover(deviceid: str, devicekey: str, log=print) -> str | None:
    host = discover_mdns(deviceid)
    if host:
        return host      # имя службы mDNS содержит device_id, этого достаточно для опознания
    log("mDNS не нашёл реле, перебираю адреса подсети")
    return discover_scan(deviceid, devicekey)


# --------------------------------------------------------------------------
# Файл с параметрами локального режима
# --------------------------------------------------------------------------
def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
