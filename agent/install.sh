#!/usr/bin/env bash
# Установка агента RPi Signage на Raspberry Pi OS Lite (Bookworm).
#
# Использование:
#   curl -fsSL https://SERVER/install.sh | sudo bash -s -- \
#     --server https://SERVER --code AB12-CD34
set -euo pipefail

SERVER=""
CODE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --server) SERVER="$2"; shift 2 ;;
    --code)   CODE="$2";   shift 2 ;;
    *) echo "Неизвестный аргумент: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$SERVER" || -z "$CODE" ]]; then
  echo "Использование: install.sh --server URL --code КОД" >&2
  exit 1
fi
if [[ "$(id -u)" -ne 0 ]]; then
  echo "Запустите через sudo." >&2
  exit 1
fi
SERVER="${SERVER%/}"

echo "==> Устанавливаю зависимости (python3, mpv)…"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
  python3 mpv curl ca-certificates

echo "==> Создаю пользователя signage…"
if ! id signage >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/signage --create-home \
          --groups video,render,input signage
fi
mkdir -p /opt/signage /var/lib/signage
chown -R signage:signage /var/lib/signage

echo "==> Скачиваю агента с $SERVER…"
curl -fsSL "$SERVER/agent.py" -o /opt/signage/agent.py
curl -fsSL "$SERVER/placeholder.png" -o /opt/signage/placeholder.png
chmod 755 /opt/signage/agent.py
# Владелец — signage: агент обновляет agent.py сам (self-update)
chown -R signage:signage /opt/signage

echo "==> Регистрирую устройство…"
sudo -u signage python3 /opt/signage/agent.py \
  --state-dir /var/lib/signage register --server "$SERVER" --code "$CODE"

echo "==> Настраиваю systemd-сервис…"
cat > /etc/systemd/system/signage-agent.service <<'UNIT'
[Unit]
Description=RPi Signage Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=signage
SupplementaryGroups=video render input
ExecStart=/usr/bin/python3 /opt/signage/agent.py run --self-update --placeholder /opt/signage/placeholder.png
Restart=always
RestartSec=5
Environment=SIGNAGE_STATE_DIR=/var/lib/signage

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now signage-agent.service

echo
echo "Готово! Агент запущен. Статус: systemctl status signage-agent"
echo "Логи:  journalctl -u signage-agent -f"
