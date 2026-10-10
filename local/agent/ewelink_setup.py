#!/usr/bin/env python3
"""Настройка управления реле через облако eWeLink.

Команды (запускать в папке агента, из виртуального окружения):

  python ewelink_setup.py login       вход в аккаунт eWeLink в браузере, сохранение токенов
  python ewelink_setup.py devices     список устройств аккаунта: device_id, состояние, Inching
  python ewelink_setup.py inching     включить Inching на реле: оно само выключится через 1 с
  python ewelink_setup.py refresh     принудительно обновить токены
  python ewelink_setup.py lan         включить управление по локальной сети: взять ключ реле из облака,
                                      найти реле в сети и проверить связь (ключ: --key, адрес: --host)

Параметры берутся из раздела `ewelink:` файла agent.yaml (appid, appsecret, redirect_url, device_id).
"""

from __future__ import annotations

import argparse
import http.server
import os
import sys
import time
import urllib.parse
import webbrowser

import yaml

import ewelink_lan
from ewelink_cloud import EweLinkClient, EweLinkError, make_nonce

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DONE_PAGE = ("<html><meta charset='utf-8'><body style='font-family:sans-serif;margin:3em'>"
             "<h2>Готово</h2><p>Вход выполнен. Эту вкладку можно закрыть, а вернуться в окно PowerShell.</p>"
             "</body></html>")


def load_cfg(path: str) -> dict:
    with open(path, encoding="utf-8-sig") as f:
        cfg = (yaml.safe_load(f) or {}).get("ewelink") or {}
    missing = [k for k in ("appid", "appsecret") if not cfg.get(k)]
    if missing:
        sys.exit(f"В разделе ewelink: файла {path} не заполнено: {', '.join(missing)}")
    return cfg


def make_client(cfg: dict) -> EweLinkClient:
    token_file = cfg.get("token_file", "ewelink_token.json")
    if not os.path.isabs(token_file):
        token_file = os.path.join(BASE_DIR, token_file)
    return EweLinkClient(str(cfg["appid"]), str(cfg["appsecret"]), token_file,
                         api_base=cfg.get("api_base"), timeout=float(cfg.get("timeout", 8)))


def parse_redirect(url: str) -> dict:
    """Достаёт code, region и state из адреса, на который облако вернуло пользователя."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return {k: q[k][0] for k in ("code", "region", "state") if k in q}


def wait_for_code(host: str, port: int, expected_state: str, timeout: float = 300) -> dict:
    """Ждёт переход браузера на redirect_url и возвращает {code, region}."""
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            got = parse_redirect(self.path)
            if "code" in got and got.get("state") == expected_state:
                result.update(got)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(DONE_PAGE.encode())
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer((host, port), Handler)
    server.timeout = 5
    deadline = time.time() + timeout
    try:
        while not result and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not result:
        raise EweLinkError(0, "вход не выполнен за 5 минут")
    return result


def cmd_login(client: EweLinkClient, cfg: dict, args) -> None:
    redirect = cfg.get("redirect_url")
    if not redirect:
        sys.exit("В разделе ewelink: файла agent.yaml не задан redirect_url "
                 "(тот же адрес, что указан в приложении на dev.ewelink.cc)")
    state = make_nonce()
    url = client.authorization_url(redirect, state)
    u = urllib.parse.urlparse(redirect)
    local = u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost") and not args.paste

    print("Откройте страницу входа eWeLink (если браузер не открылся сам), войдите в аккаунт,")
    print("где добавлено реле, и разрешите доступ:\n")
    print(url + "\n")
    if local:
        try:
            webbrowser.open(url)
        except Exception:
            pass
        print(f"Жду возврата браузера на {redirect} ...")
        got = wait_for_code(u.hostname, u.port or 80, state)
    else:
        print("После входа браузер перейдёт на адрес вида "
              f"{redirect}?code=...&region=...")
        print("Скопируйте этот адрес целиком из строки браузера и вставьте сюда. Код действует всего")
        print("30 секунд, поэтому вставьте быстро.")
        got = parse_redirect(input("Адрес: ").strip())
        if "code" not in got:
            sys.exit("В адресе нет параметра code")
    region = got.get("region") or "eu"
    client.exchange_code(got["code"], redirect, region)
    print(f"\nВход выполнен (регион {region}). Токены сохранены в {client.token_file}")
    print("Дальше: python ewelink_setup.py devices")


def cmd_devices(client: EweLinkClient, cfg: dict, args) -> None:
    devices = client.devices()
    if not devices:
        print("Устройств не найдено. Возможные причины: вход выполнен в другой аккаунт, реле не добавлено "
              "в этот аккаунт, либо ваш бесплатный APPID не открыт для этого типа устройств.")
        return
    wanted = str(cfg.get("device_id", ""))
    for d in devices:
        p = d.get("params") or {}
        mark = "  <- указано в agent.yaml" if d["deviceid"] == wanted else ""
        print(f"{d.get('name', '?')}")
        print(f"   device_id: {d['deviceid']}{mark}")
        print(f"   в сети: {'да' if d.get('online') else 'НЕТ'}   модель: {d.get('productModel', '?')}"
              f"   uiid: {(d.get('extra') or {}).get('uiid', '?')}")
        pulse, width = p.get("pulse"), p.get("pulseWidth")
        print(f"   switch: {p.get('switch', '?')}   Inching: {pulse or '?'}"
              + (f", {width / 1000:g} с" if isinstance(width, (int, float)) else ""))
        if pulse == "off" or (isinstance(width, (int, float)) and width > 2500):
            print("   ВНИМАНИЕ: для ворот нужен Inching 0,5–2,5 с (рекомендуется 1 с). "
                  "Выполните: python ewelink_setup.py inching")
    if not wanted:
        print("\nСкопируйте device_id нужного реле в agent.yaml (раздел ewelink, параметр device_id).")


def cmd_inching(client: EweLinkClient, cfg: dict, args) -> None:
    device_id = str(cfg.get("device_id") or "")
    if not device_id:
        sys.exit("Сначала укажите device_id в разделе ewelink файла agent.yaml (см. команду devices)")
    ms = args.ms
    if ms % 500 or not 500 <= ms <= 2500:
        sys.exit("Длительность должна быть 500, 1000, 1500, 2000 или 2500 мс")
    client.set_params(device_id, {"pulse": "on", "pulseWidth": ms})
    print(f"Команда отправлена: Inching включён, импульс {ms / 1000:g} с. Проверка: python ewelink_setup.py devices")


def lan_file_path(cfg: dict) -> str:
    path = cfg.get("lan_file", "ewelink_lan.json")
    return path if os.path.isabs(path) else os.path.join(BASE_DIR, path)


def cmd_lan(client: EweLinkClient, cfg: dict, args) -> None:
    device_id = str(cfg.get("device_id") or "")
    if not device_id:
        sys.exit("Сначала укажите device_id в разделе ewelink файла agent.yaml (см. команду devices)")
    key = args.key
    if not key:
        found = [d for d in client.devices() if d["deviceid"] == device_id]
        if not found:
            sys.exit(f"Устройство {device_id} не найдено в аккаунте. Проверьте device_id (команда devices)")
        key = found[0].get("devicekey")
        if not key:
            sys.exit("Облако не вернуло devicekey этого реле. Если ключ известен, передайте его: --key КЛЮЧ")
    print("Ключ реле получен." if not args.key else "Ключ взят из параметра --key.")
    host = args.host
    level = None
    if host:
        level, detail = ewelink_lan.diagnose(host, device_id, key)
        if level == "closed":
            sys.exit(f"{detail}. Проверьте адрес, что реле в сети и что в приложении eWeLink включено "
                     "«Управление по локальной сети». Если реле только что включали, перезапустите его по питанию")
        print(f"Проверка {host}: {detail}")
    else:
        print(f"Ищу реле в локальной сети (адрес этого ПК: {ewelink_lan.local_ip() or 'не определён'}), до 30 секунд ...")
        host = ewelink_lan.discover(device_id, key, log=print)
        if not host:
            sys.exit("Реле в сети не найдено. Проверьте:\n"
                     "  - ПК и реле в одной сети (одна Wi-Fi-сеть или сеть роутера, без «изоляции клиентов»);\n"
                     "  - в приложении eWeLink включено «Управление по локальной сети»;\n"
                     "  - брандмауэр Windows разрешает Python в частной сети.\n"
                     "Адрес реле также виден в списке клиентов роутера (имя вида ESP_xxxxxx); "
                     "передайте его: python ewelink_setup.py lan --host 192.168.1.50")
        level, detail = ewelink_lan.diagnose(host, device_id, key)
        print(f"Проверка {host}: {detail}")
    info = ewelink_lan.probe(host, device_id, key) or {}
    ewelink_lan.save_state(lan_file_path(cfg), {"deviceid": device_id, "devicekey": key, "host": host})
    print(f"Адрес реле {host} и ключ сохранены в {lan_file_path(cfg)} (файл секретный, не публикуйте).")
    if level != "verified":
        print("ВНИМАНИЕ: ключ ответом реле не подтверждён (на заводской прошивке это возможно). "
              "Работает ли управление по локальной сети, покажет проверка открытия ниже: в её результате "
              "должно быть «по локальной сети». Если там «облако», локальный режим не заработал.")
    pulse, width = info.get("pulse"), info.get("pulseWidth")
    if pulse is not None:
        print(f"Inching: {pulse}" + (f", {width / 1000:g} с" if isinstance(width, (int, float)) else ""))
    print("\nРекомендуется закрепить за реле этот адрес в роутере (резервирование DHCP). "
          "Если адрес всё же изменится, агент найдёт реле заново сам.")
    print("Проверка открытия ворот: python gate_agent.py --test")


def cmd_refresh(client: EweLinkClient, cfg: dict, args) -> None:
    client.refresh()
    print("Токены обновлены")


def main() -> None:
    ap = argparse.ArgumentParser(description="Настройка управления реле через облако eWeLink")
    ap.add_argument("-c", "--config", default=os.path.join(BASE_DIR, "agent.yaml"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("login", help="вход в аккаунт eWeLink")
    p.add_argument("--paste", action="store_true", help="не поднимать локальный сервер, а вставить адрес вручную")
    sub.add_parser("devices", help="список устройств")
    p = sub.add_parser("inching", help="включить Inching на реле")
    p.add_argument("ms", nargs="?", type=int, default=1000, help="длительность импульса, мс (по умолчанию 1000)")
    sub.add_parser("refresh", help="обновить токены")
    p = sub.add_parser("lan", help="включить управление по локальной сети")
    p.add_argument("--host", help="IP-адрес реле, если автопоиск не нашёл его")
    p.add_argument("--key", help="devicekey реле, если облако его не отдаёт")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    client = make_client(cfg)
    try:
        {"login": cmd_login, "devices": cmd_devices, "inching": cmd_inching, "refresh": cmd_refresh, "lan": cmd_lan}[args.cmd](
            client, cfg, args)
    except EweLinkError as e:
        sys.exit(f"Ошибка: {e}")
    except KeyboardInterrupt:
        sys.exit("Прервано")


if __name__ == "__main__":
    main()
