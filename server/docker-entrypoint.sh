#!/bin/sh
# Модели лежат в образе; кладём их в кеш пользователя, если его ещё нет.
set -e
mkdir -p "$HOME"
[ -d "$HOME/.cache" ] || cp -r /opt/model-cache "$HOME/.cache"
exec "$@"
