#!/usr/bin/env bash
# Безопасное обновление сервера БЕЗ потери данных.
# Данные в ./data (или SIGNAGE_DATA_PATH) не трогаются.
set -euo pipefail
cd "$(dirname "$0")/.."

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
