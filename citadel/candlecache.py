# -*- coding: utf-8 -*-
"""CSV-кэш свечей на диске — общий для биржевого и DEX-бота."""
from __future__ import annotations

import csv
from pathlib import Path


def safe_name(symbol: str) -> str:
    return symbol.replace("/", "-").replace(":", "-").replace("\\", "-")


def path_for(cache_dir: str, prefix: str, symbol: str, timeframe: str) -> Path:
    return Path(cache_dir) / f"{prefix}_{safe_name(symbol)}_{timeframe}.csv"


def read(path: Path) -> list[list[float]]:
    if not path.exists():
        return []
    out: list[list[float]] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if not row or row[0].startswith("ts"):
                continue
            try:
                out.append([int(float(row[0]))] + [float(x) for x in row[1:6]])
            except (ValueError, IndexError):
                continue
    return out


def write(path: Path, rows: list[list[float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        w.writerows(rows)
    tmp.replace(path)


def merge(*sources: list) -> list[list[float]]:
    """Склейка наборов свечей по времени открытия бара, поздний источник побеждает."""
    by_ts: dict[int, list[float]] = {}
    for rows in sources:
        for r in rows or []:
            if len(r) >= 6:
                by_ts[int(r[0])] = [int(r[0])] + [float(x) for x in r[1:6]]
    return [by_ts[k] for k in sorted(by_ts)]
