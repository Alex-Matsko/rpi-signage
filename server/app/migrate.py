"""Простые миграции SQLite: добавление недостающих колонок при старте.

create_all создаёт новые таблицы, но не меняет существующие — здесь
досоздаются колонки, появившиеся после v0.1.
"""
from sqlalchemy import Engine

# (таблица, колонка, SQL-тип с default)
_COLUMNS = [
    ("media_files", "transcode_status", "VARCHAR(8) NOT NULL DEFAULT 'none'"),
    ("posters", "daily_from", "VARCHAR(5)"),
    ("posters", "daily_until", "VARCHAR(5)"),
    ("posters", "weekdays_mask", "INTEGER"),
]


def run_migrations(engine: Engine) -> None:
    with engine.begin() as conn:
        for table, column, ddl in _COLUMNS:
            cols = {
                row[1]
                for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")
            }
            if cols and column not in cols:
                conn.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
                )
