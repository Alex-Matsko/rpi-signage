#!/usr/bin/env bash
# Восстановление данных из резервной копии.
# Использование: scripts/restore.sh backups/signage-data-YYYYMMDD-HHMMSS.tar.gz
set -euo pipefail
cd "$(dirname "$0")/.."

ARCHIVE="${1:-}"
DATA_PATH="${SIGNAGE_DATA_PATH:-./data}"

if [[ -z "$ARCHIVE" || ! -f "$ARCHIVE" ]]; then
  echo "Укажите файл бэкапа: scripts/restore.sh backups/signage-data-*.tar.gz" >&2
  echo "Доступные копии:" >&2
  ls -1t "${SIGNAGE_BACKUP_DIR:-./backups}"/signage-data-*.tar.gz 2>/dev/null || echo "  нет" >&2
  exit 1
fi

read -r -p "Заменить текущие данные в '$DATA_PATH' содержимым бэкапа? [y/N] " ans
[[ "$ans" == "y" || "$ans" == "Y" ]] || { echo "Отменено."; exit 0; }

echo "==> Останавливаю сервер…"
docker compose stop server || true

if [[ -d "$DATA_PATH" ]]; then
  mv "$DATA_PATH" "${DATA_PATH}.old-$(date +%s)"
fi
mkdir -p "$(dirname "$DATA_PATH")"
tar -xzf "$ARCHIVE" -C "$(dirname "$DATA_PATH")"

echo "==> Запускаю сервер…"
docker compose up -d
echo "Готово. Прежняя папка сохранена рядом как *.old-*."
