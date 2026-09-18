"""Форматери значень для показу людині: розмір у байтах, час.

Без Qt-залежностей — придатні для домену, інфраструктури й інтерфейсу
однаково.
"""

from __future__ import annotations

import time


def human(n: float) -> str:
    for u in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or u == "ТБ":
            return f"{int(n)} {u}" if u == "Б" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} ТБ"


def when(ns: int) -> str:
    # datetime.fromtimestamp().strftime() на macOS помітно дорогий при тисячах
    # рядків. localtime + ручний стабільний формат дає той самий локальний час.
    d = time.localtime(ns / 1e9)
    return (f"{d.tm_year:04d}-{d.tm_mon:02d}-{d.tm_mday:02d} "
            f"{d.tm_hour:02d}:{d.tm_min:02d}")
