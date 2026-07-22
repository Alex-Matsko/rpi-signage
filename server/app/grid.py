"""Геометрия раскладки экрана: сколько афиш и как расположены на сетке.

Та же таблица продублирована в agent/agent.py (агент — самодостаточный
stdlib-скрипт без импорта серверного кода).
"""

LAYOUTS = (1, 2, 3, 4, 6, 8)

# layout -> (rows, cols) для landscape; portrait — транспонировано,
# кроме симметричного 2x2.
_LANDSCAPE_DIMS = {
    1: (1, 1),
    2: (1, 2),
    3: (1, 3),
    4: (2, 2),
    6: (2, 3),
    8: (2, 4),
}


def grid_dims(layout: int, orientation: str) -> tuple[int, int]:
    """Возвращает (rows, cols) для раскладки layout и ориентации экрана."""
    rows, cols = _LANDSCAPE_DIMS.get(layout, (1, 1))
    if orientation == "portrait":
        rows, cols = cols, rows
    return rows, cols
