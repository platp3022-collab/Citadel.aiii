#!/usr/bin/env python3
"""
Карточка статистики картинкой.

Рисует то же, что показывает мини-апп, но в PNG: итог, плитки, сглаженную
кривую доходности и список монет, которые держим. Картинка уходит прямо
в чат — ей не нужен ни публичный адрес, ни туннель, ни браузер, поэтому
ломаться в ней нечему.

Отдельно:  python card.py   (положит data/card.png)
"""

from __future__ import annotations

import logging
import math
import sqlite3
import time
from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("card")

W, H = 1280, 860
BG = (4, 5, 10)
PANEL = (14, 16, 24)
LINE = (32, 36, 48)
INK = (242, 244, 250)
MUTED = (120, 129, 154)
UP = (34, 230, 161)
DOWN = (255, 77, 109)

FONTS = ROOT / "assets" / "fonts"


def font(size: int, weight: str = "regular") -> ImageFont.FreeTypeFont:
    names = {"regular": "Inter-Regular.ttf", "semi": "Inter-SemiBold.ttf",
             "bold": "Inter-Bold.ttf"}
    path = FONTS / names.get(weight, names["regular"])
    if path.exists():
        return ImageFont.truetype(str(path), size)
    # запасной вариант: системный шрифт с кириллицей
    for candidate in ("C:/Windows/Fonts/segoeui.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf",
                      "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def fmt_sol(v: float, d: int = 4) -> str:
    return ("+" if v >= 0 else "") + f"{v:.{d}f}"


def fmt_pct(v: float) -> str:
    return ("+" if v >= 0 else "") + f"{v:.0f}%"


def fmt_min(m: float) -> str:
    if m >= 1440:
        return f"{m / 1440:.1f}д"
    if m >= 60:
        return f"{m / 60:.1f}ч"
    return f"{m:.0f}м"


# ════════════════════════════════════════════════════════════════════════════
#  ДАННЫЕ
# ════════════════════════════════════════════════════════════════════════════

def read_state(db_path: str | Path) -> dict:
    """То же состояние, что отдаёт мини-апп, но читаем напрямую."""
    path = Path(db_path)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        return {"closed": [], "open": []}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        closed = [dict(r) for r in conn.execute(
            "SELECT * FROM trades WHERE status='closed' ORDER BY exit_ts ASC")]
        open_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM trades WHERE status='open' ORDER BY opened_ts DESC")]
    except sqlite3.Error:
        return {"closed": [], "open": []}
    finally:
        conn.close()
    return {"closed": closed, "open": open_rows}


# ════════════════════════════════════════════════════════════════════════════
#  РИСОВАНИЕ
# ════════════════════════════════════════════════════════════════════════════

def rounded(draw: ImageDraw.ImageDraw, box, radius: int, fill=None, outline=None):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=1)


def glow(img: Image.Image, box, color, radius: int = 60, alpha: int = 70):
    """Мягкое свечение — то же, что размытые пятна на веб-странице."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse(box, fill=(*color, alpha))
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(radius)))


def smooth_points(pts: Sequence[tuple[float, float]], steps: int = 12):
    """Сглаживание монотонным сплайном — как на веб-странице: линия плавная,
    но не выгибается за реальные значения."""
    n = len(pts)
    if n < 3:
        return list(pts)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    d = [(ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i]) if xs[i + 1] != xs[i] else 0.0
         for i in range(n - 1)]
    m = [d[0]]
    for i in range(1, n - 1):
        m.append(0.0 if d[i - 1] * d[i] <= 0 else (d[i - 1] + d[i]) / 2)
    m.append(d[-1])
    for i in range(n - 1):
        if d[i] == 0:
            m[i] = m[i + 1] = 0.0
            continue
        a, b = m[i] / d[i], m[i + 1] / d[i]
        h = math.hypot(a, b)
        if h > 3:
            m[i] = 3 / h * a * d[i]
            m[i + 1] = 3 / h * b * d[i]

    out = []
    for i in range(n - 1):
        x0, x1, y0, y1 = xs[i], xs[i + 1], ys[i], ys[i + 1]
        h = x1 - x0
        for s in range(steps):
            t = s / steps
            h00 = 2 * t ** 3 - 3 * t ** 2 + 1
            h10 = t ** 3 - 2 * t ** 2 + t
            h01 = -2 * t ** 3 + 3 * t ** 2
            h11 = t ** 3 - t ** 2
            out.append((x0 + t * h,
                        h00 * y0 + h10 * h * m[i] + h01 * y1 + h11 * h * m[i + 1]))
    out.append((xs[-1], ys[-1]))
    return out


def draw_chart(img: Image.Image, draw: ImageDraw.ImageDraw, box, equity: list[float]):
    x0, y0, x1, y1 = box
    if len(equity) < 2:
        draw.text(((x0 + x1) / 2, (y0 + y1) / 2), "Сделок ещё не было",
                  font=font(17, "semi"), fill=MUTED, anchor="mm")
        return

    lo, hi = min(0.0, min(equity)), max(0.0, max(equity))
    if hi - lo < 1e-9:
        hi = lo + 0.01
    pad = (hi - lo) * 0.16
    lo, hi = lo - pad, hi + pad

    def px(i):
        return x0 + i / (len(equity) - 1) * (x1 - x0)

    def py(v):
        return y1 - (v - lo) / (hi - lo) * (y1 - y0)

    # сетка и подписи шкалы
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        y = py(v)
        draw.line([(x0, y), (x1, y)], fill=(20, 23, 32), width=1)
        draw.text((x0 - 12, y), f"{v:.2f}", font=font(13), fill=MUTED, anchor="rm")

    if lo < 0 < hi:
        y = py(0)
        for seg in range(int(x0), int(x1), 10):
            draw.line([(seg, y), (seg + 5, y)], fill=(60, 66, 82), width=1)

    positive = equity[-1] >= 0
    color = UP if positive else DOWN
    curve = smooth_points([(px(i), py(v)) for i, v in enumerate(equity)])

    # заливка под кривой
    area = Image.new("RGBA", img.size, (0, 0, 0, 0))
    poly = [(x0, y1)] + [(x, y) for x, y in curve] + [(x1, y1)]
    ImageDraw.Draw(area).polygon(poly, fill=(*color, 46))
    img.alpha_composite(area)

    # свечение и сама линия
    line = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(line).line(curve, fill=(*color, 120), width=9, joint="curve")
    img.alpha_composite(line.filter(ImageFilter.GaussianBlur(7)))
    draw.line(curve, fill=color, width=3, joint="curve")

    # точка «сейчас»
    lx, ly = curve[-1]
    draw.ellipse([lx - 7, ly - 7, lx + 7, ly + 7], fill=BG)
    draw.ellipse([lx - 4.5, ly - 4.5, lx + 4.5, ly + 4.5], fill=color)


def draw_coin(img: Image.Image, draw: ImageDraw.ImageDraw, x: int, y: int,
              width: int, pos: dict):
    change = num(pos.get("change_pct"))
    up = change >= 0
    color = UP if up else DOWN
    symbol = str(pos.get("symbol") or "?")

    # значок монеты
    badge = Image.new("RGBA", img.size, (0, 0, 0, 0))
    bd = ImageDraw.Draw(badge)
    bd.rounded_rectangle([x, y + 4, x + 46, y + 50], radius=15,
                         fill=(*color, 34), outline=(*color, 150), width=2)
    img.alpha_composite(badge)
    draw.text((x + 23, y + 27), symbol[:2].upper(), font=font(16, "bold"),
              fill=color, anchor="mm")

    draw.text((x + 62, y + 8), symbol, font=font(19, "semi"), fill=INK)
    sub = (f"{num(pos.get('size_sol')):.3f} SOL  ·  держим "
           f"{fmt_min(num(pos.get('minutes')))}  ·  макс {fmt_pct(num(pos.get('high_pct')))}")
    draw.text((x + 62, y + 33), sub, font=font(14), fill=MUTED)

    value = num(pos.get("size_sol")) * (1 + change / 100)
    right = x + width
    draw.text((right, y + 6), f"{value:.4f}", font=font(19, "semi"), fill=INK, anchor="ra")
    arrow = "▲" if up else "▼"
    draw.text((right, y + 33), f"{arrow} {fmt_pct(change)}", font=font(15, "semi"),
              fill=color, anchor="ra")


def render(db_path: str | Path = "data/memebot.db",
           out: str | Path | None = None, mode: str = "paper") -> Path:
    """Собирает карточку и возвращает путь к PNG."""
    state = read_state(db_path)
    closed, open_rows = state["closed"], state["open"]

    total = sum(num(r.get("pnl_sol")) for r in closed)
    wins = [r for r in closed if num(r.get("pnl_sol")) > 0]
    day_ago = time.time() - 86400
    today = sum(num(r.get("pnl_sol")) for r in closed if num(r.get("exit_ts")) >= day_ago)

    equity, cum = [], 0.0
    for r in closed:
        cum += num(r.get("pnl_sol"))
        equity.append(cum)

    positions = []
    floating = 0.0
    for r in open_rows:
        entry, last = num(r.get("entry_price")), num(r.get("last_price"))
        change = ((last / entry - 1) * 100) if entry > 0 and last > 0 else 0.0
        high = num(r.get("high_price"))
        # проданную часть считаем по факту сделки, а не по текущей цене
        size = num(r.get("size_sol"))
        sold = max(0.0, min(100.0, num(r.get("sold_pct")))) / 100
        left = size * (1 - sold)
        floating += num(r.get("realized_sol")) - size * sold + left * change / 100
        positions.append({
            "symbol": r.get("symbol") or "—",
            "size_sol": left,
            "change_pct": change,
            "high_pct": ((high / entry - 1) * 100) if entry > 0 and high > 0 else 0.0,
            "minutes": (time.time() - num(r.get("opened_ts"))) / 60,
        })

    img = Image.new("RGBA", (W, H), (*BG, 255))
    glow(img, (-200, -260, 620, 240), (124, 92, 255), 90, 60)
    glow(img, (760, -280, 1500, 200), (34, 230, 161), 90, 42)
    glow(img, (300, 700, 1100, 1150), (255, 77, 109), 90, 30)
    draw = ImageDraw.Draw(img)

    # ── шапка ──
    draw.ellipse([44, 46, 54, 56], fill=UP)
    draw.text((66, 40), "CITADEL", font=font(17, "bold"), fill=INK)
    draw.text((66, 62), time.strftime("%d.%m.%Y  %H:%M"), font=font(14), fill=MUTED)

    tag = "РЕАЛЬНЫЕ ДЕНЬГИ" if mode == "live" else "БУМАГА"
    tag_color = DOWN if mode == "live" else MUTED
    tw = draw.textlength(tag, font=font(13, "semi"))
    rounded(draw, [W - 60 - tw - 28, 42, W - 44, 74], 16, outline=LINE)
    draw.text((W - 58 - tw / 2 - 14, 58), tag, font=font(13, "semi"), fill=tag_color, anchor="mm")

    # ── главная цифра ──
    draw.text((44, 112), "ИТОГ ЗА ВСЁ ВРЕМЯ", font=font(13, "semi"), fill=MUTED)
    big = fmt_sol(total)
    draw.text((44, 134), big, font=font(84, "bold"), fill=UP if total >= 0 else DOWN)
    bw = draw.textlength(big, font=font(84, "bold"))
    draw.text((54 + bw, 168), "SOL", font=font(24, "semi"), fill=MUTED)

    for i, (label, value, color) in enumerate([
            ("ЗА 24 ЧАСА", fmt_sol(today), UP if today >= 0 else DOWN),
            ("В ОТКРЫТЫХ", fmt_sol(floating), UP if floating >= 0 else DOWN),
            ("СДЕЛОК", str(len(closed)), INK)]):
        x = 640 + i * 200
        draw.text((x, 128), label, font=font(12, "semi"), fill=MUTED)
        draw.text((x, 150), value, font=font(26, "semi"), fill=color)

    # ── плитки ──
    winrate = len(wins) / len(closed) * 100 if closed else 0
    avg_min = (sum((num(r.get("exit_ts")) - num(r.get("opened_ts"))) / 60
                   for r in closed) / len(closed)) if closed else 0
    tiles = [
        ("ВИНРЕЙТ", f"{winrate:.0f}%", f"{len(wins)} из {len(closed)} в плюс", INK),
        ("ДЕРЖИМ СЕЙЧАС", str(len(positions)),
         f"{sum(p['size_sol'] for p in positions):.2f} SOL в рынке" if positions else "всё в кэше", INK),
        ("ЛУЧШАЯ", fmt_pct(max((num(r.get("pnl_pct")) for r in closed), default=0)),
         "по одной монете", UP),
        ("ХУДШАЯ", fmt_pct(min((num(r.get("pnl_pct")) for r in closed), default=0)),
         "по одной монете", DOWN),
        ("СРЕДНЕЕ ВРЕМЯ", fmt_min(avg_min), "в позиции", INK),
    ]
    tile_w = (W - 88 - 4 * 12) / 5
    for i, (label, value, sub, color) in enumerate(tiles):
        x = 44 + i * (tile_w + 12)
        rounded(draw, [x, 238, x + tile_w, 344], 16, fill=PANEL, outline=LINE)
        draw.text((x + 18, 256), label, font=font(12, "semi"), fill=MUTED)
        draw.text((x + 18, 280), value, font=font(28, "semi"), fill=color)
        draw.text((x + 18, 318), sub, font=font(13), fill=MUTED)

    # ── график и монеты одного размера ──
    card_w = (W - 88 - 14) / 2
    top, bottom = 366, H - 44
    rounded(draw, [44, top, 44 + card_w, bottom], 20, fill=PANEL, outline=LINE)
    rounded(draw, [44 + card_w + 14, top, W - 44, bottom], 20, fill=PANEL, outline=LINE)

    draw.text((72, top + 22), "КРИВАЯ ДОХОДНОСТИ", font=font(13, "semi"), fill=MUTED)
    note = f"{len(closed)} сделок" if closed else "ждём первую"
    draw.text((44 + card_w - 28, top + 22), note, font=font(13), fill=MUTED, anchor="ra")
    draw_chart(img, draw, (110, top + 70, 44 + card_w - 28, bottom - 40), equity)

    cx = 44 + card_w + 14
    draw.text((cx + 28, top + 22), "МОИ МОНЕТЫ", font=font(13, "semi"), fill=MUTED)
    held = sum(p["size_sol"] * (1 + p["change_pct"] / 100) for p in positions)
    draw.text((W - 72, top + 22),
              f"{len(positions)} · {held:.3f} SOL" if positions else "пусто",
              font=font(13), fill=MUTED, anchor="ra")

    if not positions:
        draw.text((cx + card_w / 2, (top + bottom) / 2), "Сейчас ничего не держим",
                  font=font(17, "semi"), fill=MUTED, anchor="mm")
    else:
        for i, pos in enumerate(positions[:5]):
            draw_coin(img, draw, cx + 28, top + 66 + i * 66, card_w - 56, pos)
        if len(positions) > 5:
            draw.text((cx + 28, top + 66 + 5 * 66), f"и ещё {len(positions) - 5}",
                      font=font(14), fill=MUTED)

    path = Path(out) if out else Path(db_path).parent / "card.png"
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(path, "PNG", optimize=True)
    return path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Карточка:", render())
