#!/usr/bin/env bash
# Ищет ВСЕ базы данных Signage на этом сервере: папки и Docker-тома.
# Показывает, сколько в каждой экранов и афиш — чтобы найти «потерянные» данные.
set -uo pipefail
cd "$(dirname "$0")/.."

count_db() {  # $1 = путь к signage.db → "N экранов, M афиш"
  python3 - "$1" <<'PY' 2>/dev/null
import sqlite3, sys
try:
    c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    d = c.execute("select count(*) from devices").fetchone()[0]
    p = c.execute("select count(*) from posters").fetchone()[0]
    print(f"{d} экранов, {p} афиш")
except Exception:
    print("не читается")
PY
}

echo "=== Папки с данными ==="
found=0
seen=""
for p in "${SIGNAGE_DATA_PATH:-./data}" ./data ../data \
         /opt/rpi-signage/data /var/lib/rpi-signage /root/rpi-signage/data \
         ~/rpi-signage/data; do
  db="$p/db/signage.db"
  [ -f "$db" ] || continue
  real=$(cd "$(dirname "$db")" && pwd)/signage.db
  case " $seen " in *" $real "*) continue ;; esac
  seen="$seen $real"
  when=$(date -r "$db" '+%Y-%m-%d %H:%M' 2>/dev/null || echo '?')
  echo "  $db"
  echo "      → $(count_db "$db"),  изменён $when"
  found=1
done
[ "$found" = 0 ] && echo "  (папок с БД не найдено)"

echo
echo "=== Docker-тома ==="
found=0
for v in $(docker volume ls -q 2>/dev/null | grep -iE 'signage|data'); do
  tmp=$(mktemp -d)
  docker run --rm -v "$v":/d -v "$tmp":/out alpine \
    sh -c 'cp /d/db/signage.db* /out/ 2>/dev/null' >/dev/null 2>&1 || true
  if [ -f "$tmp/signage.db" ]; then
    echo "  том: $v"
    echo "      → $(count_db "$tmp/signage.db")"
    found=1
  fi
  rm -rf "$tmp"
done
[ "$found" = 0 ] && echo "  (томов с БД не найдено)"

echo
echo "=== Резервные копии ==="
ls -1t ./backups/signage-data-*.tar.gz 2>/dev/null | head -5 || echo "  (нет)"

echo
echo "Нашли БД с вашими экранами/афишами? Перенесите её в рабочую папку:"
echo "  ./scripts/migrate-data.sh <имя-тома или путь-к-data>"
