#!/usr/bin/env bash
# Безопасное обновление сервера БЕЗ потери данных.
# Данные в ./data (или SIGNAGE_DATA_PATH) не трогаются.
set -euo pipefail
cd "$(dirname "$0")/.."

DATA_PATH="${SIGNAGE_DATA_PATH:-./data}"

# Защита от «пустого старта»: если рабочая папка без БД, но данные есть
# в старом Docker-томе — остановиться, чтобы не потерять контент.
if [ ! -f "$DATA_PATH/db/signage.db" ]; then
  if docker volume ls -q 2>/dev/null | grep -qiE 'signage'; then
    echo "⚠ В рабочей папке '$DATA_PATH' нет базы, но найден Docker-том с данными." >&2
    echo "  Похоже, данные остались в томе. НЕ запускаю пустой сервер." >&2
    echo "  Найдите и восстановите данные:" >&2
    echo "     ./scripts/find-data.sh" >&2
    echo "     ./scripts/migrate-data.sh <имя-тома>" >&2
    echo "  Если это первый запуск и данных ещё нет — удалите эту проверку вручную." >&2
    exit 1
  fi
fi

echo "==> Резервная копия перед обновлением…"
./scripts/backup.sh || echo "  (бэкап пропущен)"

echo "==> Забираю свежий код…"
git pull --ff-only

echo "==> Пересобираю и перезапускаю контейнер (данные сохраняются)…"
# ВНИМАНИЕ: НИКОГДА не добавляйте сюда флаг -v — он удаляет данные.
docker compose up -d --build

echo "==> Убираю старые образы…"
docker image prune -f >/dev/null || true

echo
echo "Готово. Обновлено без потери данных."
docker compose ps
