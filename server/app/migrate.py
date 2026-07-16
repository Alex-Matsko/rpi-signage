"""Простые миграции SQLite: добавление недостающих колонок и перенос данных.

create_all создаёт новые таблицы, но не меняет существующие — здесь
досоздаются колонки и переносятся данные со старых схем (v0.1/v0.2:
группы устройств и плейлисты -> города и прямые назначения афиш).
"""
from sqlalchemy import Engine

# (таблица, колонка, SQL-тип с default)
_COLUMNS = [
    ("media_files", "transcode_status", "VARCHAR(8) NOT NULL DEFAULT 'none'"),
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
]


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


def _migrate_groups_and_playlists(conn) -> None:
    """Однократный перенос данных v0.2 -> v0.3.

    Группы устройств становятся городами; назначения плейлистов
    (устройству или группе) превращаются в прямые назначения афиш
    на экран или город. Старые таблицы остаются, но не используются.
    """
    device_cols = _table_columns(conn, "devices")

    # Группы -> города
    group_to_city = {}
    if _table_exists(conn, "device_groups"):
        groups = conn.exec_driver_sql(
            "SELECT id, name, playlist_id FROM device_groups"
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

    if not _table_exists(conn, "playlist_items"):
        return

    def playlist_posters(pl_id):
        return [r[0] for r in conn.exec_driver_sql(
            "SELECT poster_id FROM playlist_items WHERE playlist_id = ? "
            "ORDER BY position", (pl_id,)
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
