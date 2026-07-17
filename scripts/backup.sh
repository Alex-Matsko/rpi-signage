#!/usr/bin/env bash
# Резервная копия всех данных сервера (БД, медиа, скриншоты, ключ сессий).
set -euo pipefail
cd "$(dirname "$0")/.."

DATA_PATH="${SIGNAGE_DATA_PATH:-./data}"
BACKUP_DIR="${SIGNAGE_BACKUP_DIR:-./backups}"

if [[ ! -d "$DATA_PATH" ]]; then
  echo "Папка данных '$DATA_PATH' не найдена — нечего сохранять." >&2
  exit 0
fi

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="$BACKUP_DIR/signage-data-$STAMP.tar.gz"

tar -czf "$ARCHIVE" -C "$(dirname "$DATA_PATH")" "$(basename "$DATA_PATH")"
echo "Бэкап сохранён: $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"

# Оставляем 10 последних копий
ls -1t "$BACKUP_DIR"/signage-data-*.tar.gz 2>/dev/null | tail -n +11 | xargs -r rm -f
