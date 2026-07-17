# Продовое развёртывание (Docker-хост + HAProxy на OPNsense)

Инструкция для боевого сервера: Docker-контейнер за reverse-proxy HAProxy
(OPNsense), с HTTPS, устойчивым хранением данных и автозапуском.

Схема:

```
Устройства (RPi/NUC) ─┐
Браузеры админов ─────┤→  OPNsense (HAProxy, TLS) ─→  Docker-хост:8080 ─→ контейнер:8000
```

---

## 1. Docker-хост: разворачиваем сервер

На Docker-хосте (Linux с Docker и docker compose):

```bash
# 1. Клонируем в СТАБИЛЬНУЮ папку (не удаляйте и не переклонируйте её потом)
sudo mkdir -p /opt/rpi-signage && sudo chown "$USER" /opt/rpi-signage
git clone https://github.com/Alex-Matsko/rpi-signage.git /opt/rpi-signage
cd /opt/rpi-signage

# 2. Настройки
cp .env.example .env
nano .env
```

Заполните `.env` (главное — **абсолютный** путь к данным и пароль):

```ini
ADMIN_PASSWORD=длинный-надёжный-пароль
SIGNAGE_DATA_PATH=/opt/rpi-signage/data     # абсолютный! данные не потеряются
SIGNAGE_PORT=8080                            # порт на Docker-хосте для HAProxy
TZ=Europe/Moscow
SIGNAGE_MAX_UPLOAD_MB=1024
# Если хотите сузить доверие к прокси — укажите IP OPNsense вместо *:
# SIGNAGE_FORWARDED_ALLOW_IPS=192.168.1.1
```

Запуск:

```bash
docker compose up -d
docker compose ps          # server — running/healthy
curl -s http://localhost:8080/healthz    # {"ok":true,"version":"..."}
```

Контейнер слушает `SIGNAGE_PORT` (8080) на всех интерфейсах Docker-хоста —
именно сюда пойдёт HAProxy. Данные лежат в `/opt/rpi-signage/data`.

---

## 2. OPNsense: HAProxy → сервер

Firewall → Services → **HAProxy**.

### 2.1 Real Server (backend-сервер)

- **Name:** `signage-docker`
- **FQDN or IP:** IP вашего Docker-хоста (например `192.168.1.50`)
- **Port:** `8080`
- **SSL:** выкл (TLS терминируется на HAProxy; до контейнера — http)

### 2.2 Backend Pool

- **Name:** `signage-backend`
- **Servers:** `signage-docker`

### 2.3 Public Service (frontend)

- **Listen:** внешний адрес:443, **Type: HTTP/HTTPS (SSL offloading)**
- **SSL Offloading:** включить, выбрать сертификат для вашего домена
  (Let's Encrypt через ACME-плагин OPNsense или свой).
- **Default Backend Pool:** `signage-backend`

### 2.4 Важные опции (иначе не будут работать терминал и большие видео)

В **Public Service → Advanced** (или в HAProxy → Settings → «Custom options»)
добавьте:

```
# WebSocket для веб-терминала (SSH из браузера) + долгие сессии
timeout tunnel 1h
timeout client 5m
timeout server 5m

# Пробрасываем реальную схему/адрес, чтобы ссылки строились как https://<домен>
http-request set-header X-Forwarded-Proto https
option forwardfor
```

Пояснения:
- `timeout tunnel 1h` — держит WebSocket терминала открытым (иначе рвётся).
- `X-Forwarded-Proto https` — сервер уже настроен доверять этому заголовку,
  поэтому команда установки и адрес терминала строятся с `https://<домен>`.
- HAProxy по умолчанию **не ограничивает размер тела запроса**, поэтому
  загрузка видео проходит. Если поставили лимит (`tune.h2.*` / `option
  http-buffer-request` с ограничениями) — снимите его для этого сервиса.

> HAProxy в HTTP-режиме пропускает WebSocket-апгрейд автоматически. Убедитесь,
> что сервис работает как **HTTP**, а не TCP-режим (для правильных заголовков).

### 2.5 Firewall

Разрешите с OPNsense доступ к Docker-хосту на порт `8080` (LAN). Наружу
открыт только `443` на OPNsense. Порт `8080` контейнера **не публикуйте**
в интернет напрямую.

---

## 3. Проверка

1. Откройте `https://<ваш-домен>` — веб-интерфейс, вход `admin` / пароль.
2. **Экраны → создайте город и экран** — на странице экрана команда установки
   должна быть с **https://<ваш-домен>** (а не внутренним IP). Это признак,
   что заголовки прокси проходят правильно.
3. Установите агент на устройство (см. [INSTALL.md](INSTALL.md)) — экран
   станет online.
4. На странице экрана нажмите **«⌨ Открыть терминал»** — если открылся живой
   shell, WebSocket через HAProxy работает.
5. Загрузите видео в медиатеку — если проходит, лимиты HAProxy в порядке.

---

## 4. Резервные копии (обязательно)

Настройте ежедневный бэкап данных:

```bash
crontab -e
```
```
0 3 * * * cd /opt/rpi-signage && ./scripts/backup.sh >> /var/log/signage-backup.log 2>&1
```

Копии складываются в `/opt/rpi-signage/backups/` (хранятся 10 последних).
Восстановление: `./scripts/restore.sh backups/signage-data-*.tar.gz`.

---

## 5. Обновления без потери данных

```bash
cd /opt/rpi-signage
./scripts/update.sh
```

Скрипт делает бэкап, забирает свежий код и пересобирает контейнер — **данные
в `/opt/rpi-signage/data` не трогаются**. Устройства обновят агент сами.

> ⚠️ Никогда не используйте `docker compose down -v` и не переклонируйте
> проект в новую папку — данные привязаны к `SIGNAGE_DATA_PATH`.

---

## 6. Автозапуск и обслуживание

- `restart: unless-stopped` в compose — контейнер сам поднимается после
  перезагрузки Docker-хоста.
- Логи сервера: `docker compose logs -f server`.
- Состояние: `docker compose ps`, `curl -s http://localhost:8080/healthz`.
- Если данные «пропали» после манипуляций — не паникуйте, они на диске:
  `./scripts/find-data.sh` найдёт их, `./scripts/migrate-data.sh` вернёт.
