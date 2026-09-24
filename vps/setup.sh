#!/bin/sh
# Первичная настройка VPS (Ubuntu/Debian). Запуск из папки vps:  sudo sh setup.sh
set -e
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
    echo "Устанавливаю Docker..."
    curl -fsSL https://get.docker.com | sh
fi

if [ ! -f .env ]; then
    cp .env.example .env
    sed -i "s/^B24_BOT_TOKEN=.*/B24_BOT_TOKEN=$(openssl rand -hex 16)/" .env
    sed -i "s/^LINK_TOKEN=.*/LINK_TOKEN=$(openssl rand -hex 32)/" .env
    chmod 600 .env
    echo
    echo "Создан файл .env. Заполните в нём SITE_ADDRESS, B24_WEBHOOK_URL и B24_USER_IDS:"
    echo "    nano .env"
    echo "затем снова запустите: sudo sh setup.sh"
    exit 0
fi

mkdir -p data/kuma data/bot data/caddy
docker compose up -d --build
echo
echo "Готово. Проверка:  docker compose ps   и   docker compose logs -f bot"
echo "LINK_TOKEN для файла local/.env на компьютере у ворот:"
grep '^LINK_TOKEN=' .env | cut -d= -f2
