#!/usr/bin/env bash
# Установка агента RPi Signage на Raspberry Pi OS Lite (Bookworm).
#
# Использование:
#   curl -fsSL https://SERVER/install.sh | sudo bash -s -- \
#     --server https://SERVER --code AB12-CD34
set -euo pipefail

SERVER=""
CODE=""
WEB_PASSWORD=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --server)       SERVER="$2";       shift 2 ;;
    --code)         CODE="$2";         shift 2 ;;
    --web-password) WEB_PASSWORD="$2"; shift 2 ;;
    *) echo "Неизвестный аргумент: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$SERVER" ]]; then
  echo "Использование: install.sh --server URL [--code КОД] [--web-password ПАРОЛЬ]" >&2
  echo "Без --code устройство можно привязать позже через веб-панель (:8088)." >&2
  exit 1
fi
if [[ "$(id -u)" -ne 0 ]]; then
  echo "Запустите через sudo." >&2
  exit 1
fi
SERVER="${SERVER%/}"

echo "==> Устанавливаю зависимости (python3, mpv, network-manager, alsa)…"
apt-get update -qq
# network-manager — Wi-Fi через nmcli; alsa-utils — звук напрямую через ALSA
# (headless-система без сессии PipeWire; mpv выводит звук через ALSA).
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
  python3 mpv curl ca-certificates network-manager alsa-utils || \
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
  python3 mpv curl ca-certificates

echo "==> Создаю пользователя signage…"
if ! id signage >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/signage --create-home \
          --groups video,render,input,audio signage
fi
# Доступ к звуковым устройствам (на случай, если пользователь уже существовал)
usermod -aG audio signage 2>/dev/null || true
mkdir -p /opt/signage /var/lib/signage
chown -R signage:signage /var/lib/signage

echo "==> Скачиваю агента с $SERVER…"
curl -fsSL "$SERVER/agent.py" -o /opt/signage/agent.py
curl -fsSL "$SERVER/placeholder.png" -o /opt/signage/placeholder.png
curl -fsSL "$SERVER/waiting_bg.png" -o /opt/signage/waiting_bg.png
chmod 755 /opt/signage/agent.py
# Владелец — signage: агент обновляет agent.py сам (self-update)
chown -R signage:signage /opt/signage

echo "==> Разрешаю управление системой из панели…"
# Пользователь signage может перезагрузить устройство, менять имя хоста и
# управлять NetworkManager без пароля (для кнопок в панели/интерфейсе).
cat > /etc/sudoers.d/signage <<'SUDO'
signage ALL=(root) NOPASSWD: /sbin/reboot, /usr/sbin/reboot, \
  /usr/bin/hostnamectl, /usr/bin/nmcli, /usr/bin/timedatectl
SUDO
chmod 440 /etc/sudoers.d/signage
# Разрешаем пользователю signage управлять NetworkManager напрямую
usermod -aG netdev signage 2>/dev/null || true

if [[ -n "$CODE" ]]; then
  echo "==> Регистрирую устройство…"
  sudo -u signage python3 /opt/signage/agent.py \
    --state-dir /var/lib/signage register --server "$SERVER" --code "$CODE"
else
  echo "==> Код не задан — привяжете устройство позже через веб-панель :8088."
fi

if [[ -n "$WEB_PASSWORD" ]]; then
  echo "==> Задаю пароль локальной веб-панели…"
  sudo -u signage python3 /opt/signage/agent.py \
    --state-dir /var/lib/signage set-password --password "$WEB_PASSWORD"
fi

echo "==> Прячу системную консоль на экране…"
# На ТВ не должны просвечивать мастер создания пользователя Raspberry Pi OS
# (синий экран «please enter the username») и приглашение логина tty1 —
# они видны в моменты, когда mpv перезапускается или ещё не поднялся.
systemctl disable --now userconfig.service 2>/dev/null || true
systemctl disable --now getty@tty1.service 2>/dev/null || true

echo "==> Настраиваю systemd-сервис…"
cat > /etc/systemd/system/signage-agent.service <<'UNIT'
[Unit]
Description=RPi Signage Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=signage
SupplementaryGroups=video render input audio
ExecStart=/usr/bin/python3 /opt/signage/agent.py run --self-update --allow-system --placeholder /opt/signage/placeholder.png --awaiting-background /opt/signage/waiting_bg.png
Restart=always
RestartSec=5
Environment=SIGNAGE_STATE_DIR=/var/lib/signage

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now signage-agent.service

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Готово! Агент запущен."
echo "  Статус:        systemctl status signage-agent"
echo "  Логи:          journalctl -u signage-agent -f"
echo "  Панель устройства: http://${IP:-<IP-адрес>}:8088  (логин admin)"
if [[ -z "$WEB_PASSWORD" ]]; then
  echo "  Пароль панели по умолчанию: signage — смените его в разделе «Система»."
fi
