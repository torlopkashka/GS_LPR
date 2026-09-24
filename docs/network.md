# Сеть: как VPS получает видео с камер

Камеры G4 Pro подключены к консоли UniFi Protect (UDM / UCG / Cloud Key / UNVR) в сети
объекта. Сервер распознавания стоит в дата-центре, поэтому ему нужен доступ
к RTSP-потокам. Самый простой и безопасный путь: туннель **WireGuard**, который
поднимает компьютер у ворот. Белый IP и проброс портов на объекте не нужны.

```
 [G4 Pro]──┐                                     ┌──────── VPS ────────┐
 [G4 Pro]──┼─ UniFi Protect ── LAN ── ПК у ворот ═══ WireGuard ═══ wg0 10.8.0.1 │
           │  192.168.1.1        (10.8.0.2, NAT)  │   lpr (Docker)       │
 [RB600]◄──┴──────── реле ◄─── агент ─── wss ─────┤   caddy :443         │
                                                  └──────────────────────┘
```

## 1. Включить RTSP в UniFi Protect

Protect → **Devices** → камера → **Settings → Advanced → RTSP**. Включите
поток **Medium** (1280×720): его хватает для номеров и он экономит канал.
Если машины далеко, берите **High**.

Protect покажет адрес вида:

```
rtsps://192.168.1.1:7441/AbCdEf1234567890?enableSrtp
```

В `config.yaml` запишите незашифрованный вариант (порт 7447, без `?enableSrtp`):

```
rtsp://192.168.1.1:7447/AbCdEf1234567890
```

Внутри туннеля WireGuard трафик всё равно шифруется.

Проверка с любого компьютера в сети объекта: `ffplay rtsp://192.168.1.1:7447/...` или VLC.

## 2. WireGuard

### На VPS (Ubuntu/Debian)
```bash
apt install wireguard
cd /etc/wireguard && umask 077
wg genkey | tee vps.key | wg pubkey > vps.pub
# скопируйте deploy/wireguard/wg0-vps.conf в /etc/wireguard/wg0.conf и подставьте ключи
ufw allow 51820/udp   # если включён ufw
systemctl enable --now wg-quick@wg0
```

### На компьютере у ворот (Linux)
```bash
apt install wireguard iptables
cd /etc/wireguard && umask 077
wg genkey | tee gate.key | wg pubkey > gate.pub
# deploy/wireguard/wg0-gate.conf → /etc/wireguard/wg0.conf
# замените eth0 на имя сетевого интерфейса (ip -br a)
systemctl enable --now wg-quick@wg0
```

Проверка на VPS:
```bash
wg show                     # есть "latest handshake"
ping 10.8.0.2
ping 192.168.1.1            # консоль UniFi через туннель
ffprobe rtsp://192.168.1.1:7447/AbCd...   # поток доступен
```

Подсеть `192.168.1.0/24` замените на свою в обоих конфигах.

### Альтернатива: WireGuard на шлюзе UniFi
Если на объекте белый IP, можно поднять **WireGuard VPN Server** прямо на UDM/UCG
(Settings → VPN → VPN Server → WireGuard), а VPS подключить к нему как клиента.
Тогда компьютер у ворот нужен только для агента.

### Если компьютер у ворот на Windows
Поставьте официальный клиент WireGuard. Для доступа VPS к LAN включите на Windows
общий доступ (ICS) или маршрутизацию. Проще поставить на объекте Raspberry Pi / мини-ПК на Linux.

## 3. Канал связи

Поток Medium занимает 1,5–3 Мбит/с на камеру, итого до ~6 Мбит/с исходящего
трафика с объекта, постоянно. Если канал узкий:
- снизьте битрейт/FPS потока в настройках камеры в Protect;
- или запускайте распознавание на объекте (см. README, «Вариант размещения»).

## 4. Агент ворот

Агент сам подключается к серверу по `wss://ваш-домен/ws/agent` (через Caddy/HTTPS).
Можно и через туннель: `server_url: ws://10.8.0.1:8000`. Тогда в `docker-compose.yml`
откройте порт `"10.8.0.1:8000:8000"`.
