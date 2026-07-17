#!/usr/bin/env bash
# Переносит данные Signage из источника в текущую рабочую папку сервера.
# Источник: имя Docker-тома ИЛИ путь к папке data (см. ./scripts/find-data.sh).
set -euo pipefail
cd "$(dirname "$0")/.."

SRC="${1:-}"
DEST="${SIGNAGE_DATA_PATH:-./data}"
if [ -z "$SRC" ]; then
  echo "Использование: ./scripts/migrate-data.sh <docker-том | путь-к-data>" >&2
  echo "Сначала найдите источник: ./scripts/find-data.sh" >&2
  exit 1
fi

mkdir -p "$DEST"
DEST_ABS="$(cd "$DEST" && pwd)"

echo "==> Останавливаю сервер…"
docker compose stop server 2>/dev/null || true

if [ -f "$DEST_ABS/db/signage.db" ]; then
  echo "==> В целевой папке уже есть БД — сохраняю её копию рядом."
  cp -a "$DEST_ABS" "${DEST_ABS}.before-migrate-$(date +%s)"
fi

if docker volume inspect "$SRC" >/dev/null 2>&1; then
  echo "==> Копирую из Docker-тома '$SRC' → $DEST_ABS"
  docker run --rm -v "$SRC":/from -v "$DEST_ABS":/to alpine \
    sh -c 'cp -a /from/. /to/'
elif [ -d "$SRC" ]; then
  echo "==> Копирую из папки '$SRC' → $DEST_ABS"
  cp -a "$SRC/." "$DEST_ABS/"
else
  echo "Источник '$SRC' не найден (ни Docker-том, ни папка)." >&2
  exit 1
fi

echo "==> Запускаю сервер…"
docker compose up -d
echo "Готово. Проверьте, что экраны и афиши вернулись."
