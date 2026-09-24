#!/bin/sh
# Установка Uptime Kuma на VPS (Ubuntu/Debian). Запуск из папки vps:  sudo sh setup.sh
set -e
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
    echo "Устанавливаю Docker..."
    curl -fsSL https://get.docker.com | sh
fi

# Порт 3001: веб-интерфейс Kuma и приём сигналов с объекта
if command -v ufw >/dev/null 2>&1; then
    ufw allow 22/tcp >/dev/null
    ufw allow 3001/tcp >/dev/null
fi

mkdir -p data
docker compose up -d

IP=$(curl -fs4 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
echo
echo "Готово. Откройте в браузере:  http://$IP:3001"
echo "Дальше — раздел «Настройка Uptime Kuma» в docs/vps.md"
