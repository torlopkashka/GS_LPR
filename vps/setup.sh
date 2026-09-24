#!/bin/sh
# Установка на VPS (Ubuntu/Debian): Uptime Kuma и бот Битрикс24.
# Запуск из папки vps:  sudo sh setup.sh
set -e
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
    echo "Устанавливаю Docker..."
    curl -fsSL https://get.docker.com | sh
fi

if [ ! -f .env ]; then
    cp .env.example .env
    sed -i "s/^B24_BOT_TOKEN=.*/B24_BOT_TOKEN=$(openssl rand -hex 16)/" .env
    sed -i "s/^SITE_TOKEN=.*/SITE_TOKEN=$(openssl rand -hex 32)/" .env
    chmod 600 .env
    echo
    echo "Создан файл .env. Впишите в него B24_WEBHOOK_URL и B24_USER_IDS:"
    echo "    nano .env"
    echo "затем снова запустите: sudo sh setup.sh"
    exit 0
fi

# Порты: 3001 — Uptime Kuma, 8080 — бот (сюда обращается ПК у ворот)
if command -v ufw >/dev/null 2>&1; then
    ufw allow 22/tcp >/dev/null
    ufw allow 3001/tcp >/dev/null
    ufw allow 8080/tcp >/dev/null
fi

mkdir -p data/kuma data/bot
docker compose up -d --build

IP=$(curl -fs4 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
echo
echo "Готово."
echo "  Uptime Kuma:            http://$IP:3001"
echo "  Бот, проверка:          http://$IP:8080/healthz"
echo "  Для local/.env на ПК у ворот:"
echo "    VPS_URL=http://$IP:8080"
echo "    VPS_TOKEN=$(grep '^SITE_TOKEN=' .env | cut -d= -f2)"
