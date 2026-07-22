"""Простые миграции SQLite: добавление недостающих колонок и перенос данных.

create_all создаёт новые таблицы, но не меняет существующие — здесь
досоздаются колонки и переносятся данные со старых схем (v0.1/v0.2:
группы устройств и плейлисты -> города и прямые назначения афиш; v0.4:
плейлисты/группы возвращены как отдельные таблицы — см.
rename_legacy_group_playlist_tables и _backfill_user_cities).
"""
from sqlalchemy import Engine

# (таблица, колонка, SQL-тип с default)
_COLUMNS = [
    ("media_files", "transcode_status", "VARCHAR(8) NOT NULL DEFAULT 'none'"),
    ("media_files", "uploaded_by", "INTEGER"),
    ("posters", "daily_from", "VARCHAR(5)"),
    ("posters", "daily_until", "VARCHAR(5)"),
    ("posters", "weekdays_mask", "INTEGER"),
    ("posters", "sort_order", "INTEGER NOT NULL DEFAULT 0"),
    ("posters", "created_by", "INTEGER"),
    ("users", "role", "VARCHAR(16) NOT NULL DEFAULT 'admin'"),
    ("users", "city_id", "INTEGER"),
    ("devices", "city_id", "INTEGER"),
    ("devices", "screenshot_at", "DATETIME"),
    ("devices", "local_ip", "VARCHAR(45)"),
    ("devices", "web_port", "INTEGER"),
    ("devices", "orientation", "VARCHAR(10) NOT NULL DEFAULT 'landscape'"),
    ("devices", "grid_layout", "INTEGER NOT NULL DEFAULT 1"),
    ("devices", "grid_images_only", "BOOLEAN NOT NULL DEFAULT 1"),
]

# v0.1/v0.2 таблицы групп/плейлистов, оставшиеся от _migrate_groups_and_playlists
# (переносятся данные, но сами таблицы не удаляются). С v0.4 эти же имена
# заняты новыми моделями DeviceGroup/Playlist/PlaylistItem — если старая
# таблица физически существует, create_all её не тронет и код молча
# привяжется к несовместимой схеме. Поэтому переименовываем их в сторону
# ДО create_all (см. rename_legacy_group_playlist_tables, main.py).
_LEGACY_GROUP_PLAYLIST_TABLES = ["device_groups", "playlists", "playlist_items"]


def _table_columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}


def _table_exists(conn, table: str) -> bool:
    return bool(conn.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def _migration_done(conn, name: str) -> bool:
    conn.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS migrations_done (name TEXT PRIMARY KEY)"
    )
    return bool(conn.exec_driver_sql(
        "SELECT 1 FROM migrations_done WHERE name = ?", (name,)
    ).fetchone())


def _mark_migration(conn, name: str) -> None:
    conn.exec_driver_sql(
        "INSERT OR IGNORE INTO migrations_done (name) VALUES (?)", (name,)
    )


def rename_legacy_group_playlist_tables(engine: Engine) -> None:
    """Разово, ДО create_all: уводит в сторону старые (v0.1/v0.2) таблицы
    device_groups/playlists/playlist_items, если они физически остались в
    базе после переноса данных на v0.3 (_migrate_groups_and_playlists их
    читает, но никогда не удаляет). Иначе create_all не сможет создать
    новые одноимённые таблицы с v0.4-схемой — create_all не трогает уже
    существующие таблицы, и код молча привяжется к старой несовместимой
    форме (упадёт на первой же вставке: "no such column").

    На свежей базе (без легаси-таблиц) — no-op, безопасно вызывать всегда.
    """
    with engine.begin() as conn:
        if _migration_done(conn, "v04_rename_legacy_tables"):
            return
        for table in _LEGACY_GROUP_PLAYLIST_TABLES:
            if _table_exists(conn, table):
                conn.exec_driver_sql(
                    f"ALTER TABLE {table} RENAME TO legacy_{table}_v1"
                )
        _mark_migration(conn, "v04_rename_legacy_tables")


def run_migrations(engine: Engine) -> None:
    with engine.begin() as conn:
        for table, column, ddl in _COLUMNS:
            cols = _table_columns(conn, table)
            if cols and column not in cols:
                conn.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
                )
        if not _migration_done(conn, "v03_groups_playlists"):
            _migrate_groups_and_playlists(conn)
            _mark_migration(conn, "v03_groups_playlists")
        if not _migration_done(conn, "v04_user_cities_backfill"):
            _backfill_user_cities(conn)
            _mark_migration(conn, "v04_user_cities_backfill")


def _backfill_user_cities(conn) -> None:
    """v0.4: разовый перенос User.city_id (единственный город) в user_cities
    (многие-ко-многим). Колонка users.city_id остаётся как подсказка
    основного города, но проверки доступа её больше не используют."""
    for uid, cid in conn.exec_driver_sql(
        "SELECT id, city_id FROM users WHERE city_id IS NOT NULL"
    ).fetchall():
        conn.exec_driver_sql(
            "INSERT OR IGNORE INTO user_cities (user_id, city_id) VALUES (?, ?)",
            (uid, cid),
        )


def _migrate_groups_and_playlists(conn) -> None:
    """Однократный перенос данных v0.2 -> v0.3.

    Группы устройств становятся городами; назначения плейлистов
    (устройству или группе) превращаются в прямые назначения афиш
    на экран или город.

    Читает из legacy_device_groups_v1/legacy_playlist_items_v1, а не из
    device_groups/playlist_items напрямую: rename_legacy_group_playlist_tables
    (вызывается ДО create_all, см. main.py) к этому моменту уже увёл старые
    таблицы под эти имена, если они физически существовали — иначе к моменту
    вызова этой функции (после create_all) под именами device_groups/
    playlist_items уже лежат НОВЫЕ пустые таблицы v0.4-схемы (DeviceGroup/
    PlaylistItem), и чтение из них дало бы неверные данные или упало на
    "no such column".
    """
    device_cols = _table_columns(conn, "devices")

    # Группы -> города
    group_to_city = {}
    if _table_exists(conn, "legacy_device_groups_v1"):
        groups = conn.exec_driver_sql(
            "SELECT id, name, playlist_id FROM legacy_device_groups_v1"
        ).fetchall()
        for gid, name, _pl in groups:
            cur = conn.exec_driver_sql(
                "INSERT INTO cities (name, created_at) "
                "VALUES (?, datetime('now', 'localtime'))",
                (name,),
            )
            group_to_city[gid] = cur.lastrowid
        if "group_id" in device_cols:
            for gid, city_id in group_to_city.items():
                conn.exec_driver_sql(
                    "UPDATE devices SET city_id = ? WHERE group_id = ?",
                    (city_id, gid),
                )
    else:
        groups = []

    if not _table_exists(conn, "legacy_playlist_items_v1"):
        return

    def playlist_posters(pl_id):
        return [r[0] for r in conn.exec_driver_sql(
            "SELECT poster_id FROM legacy_playlist_items_v1 "
            "WHERE playlist_id = ? ORDER BY position", (pl_id,)
        )]

    # Плейлист устройства -> назначения афиш на экран
    if "playlist_id" in device_cols:
        for dev_id, pl_id in conn.exec_driver_sql(
            "SELECT id, playlist_id FROM devices WHERE playlist_id IS NOT NULL"
        ).fetchall():
            for poster_id in playlist_posters(pl_id):
                conn.exec_driver_sql(
                    "INSERT INTO poster_targets (poster_id, device_id) "
                    "VALUES (?, ?)", (poster_id, dev_id),
                )
    # Плейлист группы -> назначения афиш на город
    for gid, _name, pl_id in groups:
        if pl_id is None:
            continue
        for poster_id in playlist_posters(pl_id):
            conn.exec_driver_sql(
                "INSERT INTO poster_targets (poster_id, city_id) VALUES (?, ?)",
                (poster_id, group_to_city[gid]),
            )
