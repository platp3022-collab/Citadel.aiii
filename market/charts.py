"""Графики брифа: дневные свечи и относительная динамика вотчлиста.

SVG собирается на месте, без библиотек — страница остаётся самодостаточной.
Цвета берутся из CSS-токенов, поэтому оба графика живут в светлой и тёмной теме.

Палитра сравнения — категориальные слоты 1,2,3,4,5,7 стандартной схемы.
Слоты 6 (зелёный) и 8 (красный) намеренно пропущены: на этой странице
зелёный и красный уже заняты семантикой направления (лонг/шорт, рост/падение),
и линия тикера такого же цвета читалась бы как оценка, а не как идентификатор.
Набор проверен валидатором в обеих темах против поверхностей страницы.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Sequence

# ════════════════════════════════════════════════════════════════════════════
#  СТИЛИ И СКРИПТ
# ════════════════════════════════════════════════════════════════════════════

CHART_CSS = """
.chart-card {
  background: var(--surface);
  border: 1px solid var(--rule-soft);
  border-radius: 4px;
  box-shadow: var(--shadow);
  overflow: hidden;
}

.chart-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px 14px;
  padding: 15px 18px 13px;
  border-bottom: 1px solid var(--rule-soft);
}
.chart-head .t {
  font-family: Archivo, sans-serif;
  font-size: 16px;
  font-weight: 700;
  letter-spacing: -.012em;
}
.chart-head .sub { font-size: 13px; color: var(--ink-soft); }
.chart-head .right {
  margin-left: auto;
  font-family: "IBM Plex Mono", monospace;
  font-variant-numeric: tabular-nums;
  font-size: 12px;
  color: var(--ink-faint);
}

.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  padding: 11px 18px;
  border-bottom: 1px solid var(--rule-soft);
  font-family: "IBM Plex Mono", monospace;
  font-size: 11.5px;
  color: var(--ink-soft);
}
.legend i {
  display: inline-block;
  width: 14px;
  height: 2px;
  border-radius: 1px;
  margin-right: 6px;
  vertical-align: 3px;
}
.legend .key { white-space: nowrap; }
.legend .dash {
  height: 0;
  border-top: 2px dashed currentColor;
  background: none;
}

/* Узкий экран: график скроллится внутри себя, иначе подписи осей нечитаемы. */
.chart-scroll { overflow-x: auto; position: relative; }
.chart-svg { display: block; width: 100%; min-width: 680px; height: auto; }

.grid-line { stroke: var(--rule-soft); stroke-width: 1; }
.axis-line { stroke: var(--rule); stroke-width: 1; }
.axis-text {
  fill: var(--ink-faint);
  font-family: "IBM Plex Mono", monospace;
  font-size: 10.5px;
  font-variant-numeric: tabular-nums;
}
.lvl-line { stroke-width: 1.4; stroke-dasharray: 5 4; fill: none; }
.lvl-text {
  font-family: "IBM Plex Mono", monospace;
  font-size: 9.5px;
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}
.ma-line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.series-line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.series-label {
  font-family: "IBM Plex Mono", monospace;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}
.base-line { stroke: var(--ink-faint); stroke-width: 1; stroke-dasharray: 3 3; }

.crosshair { stroke: var(--ink-faint); stroke-width: 1; stroke-dasharray: 3 3; pointer-events: none; }
.hover-dot { pointer-events: none; }

.tip {
  position: absolute;
  pointer-events: none;
  opacity: 0;
  /* Растёт вниз от верха поля: контейнер со скроллом обрезал бы всё, что выше. */
  transform: translate(-50%, 0);
  background: var(--surface);
  border: 1px solid var(--rule);
  border-radius: 3px;
  box-shadow: var(--shadow);
  padding: 8px 11px;
  font-family: "IBM Plex Mono", monospace;
  font-variant-numeric: tabular-nums;
  font-size: 11.5px;
  line-height: 1.65;
  color: var(--ink);
  white-space: nowrap;
  z-index: 5;
}
.tip .d { color: var(--ink-faint); margin-bottom: 3px; }
.tip .row { display: flex; gap: 10px; justify-content: space-between; }
.tip .row span:first-child { color: var(--ink-soft); }
.tip .sw {
  display: inline-block; width: 8px; height: 8px; border-radius: 2px;
  margin-right: 6px; vertical-align: 0;
}
"""

CHART_JS = """
(function () {
  function fmt(v) {
    var a = Math.abs(v);
    if (a >= 1000) return v.toLocaleString('ru-RU', {minimumFractionDigits: 2, maximumFractionDigits: 2});
    if (a >= 1) return v.toFixed(2);
    return v.toFixed(4);
  }

  document.querySelectorAll('.chart-scroll').forEach(function (box) {
    var svg = box.querySelector('.chart-svg');
    if (!svg) return;
    var raw = svg.getAttribute('data-chart');
    if (!raw) return;
    var spec;
    try { spec = JSON.parse(raw); } catch (e) { return; }

    var tip = document.createElement('div');
    tip.className = 'tip';
    box.appendChild(tip);

    var cross = svg.querySelector('.crosshair');
    var dots = svg.querySelectorAll('.hover-dot');

    function hide() {
      tip.style.opacity = 0;
      if (cross) cross.setAttribute('opacity', 0);
      dots.forEach(function (d) { d.setAttribute('opacity', 0); });
    }

    function move(ev) {
      var r = svg.getBoundingClientRect();
      if (!r.width) return;
      var scale = spec.vw / r.width;
      var x = (ev.clientX - r.left) * scale;
      var i = Math.round((x - spec.x0) / spec.step);
      if (i < 0) i = 0;
      if (i >= spec.n) i = spec.n - 1;
      var cx = spec.x0 + i * spec.step;

      if (cross) {
        cross.setAttribute('x1', cx);
        cross.setAttribute('x2', cx);
        cross.setAttribute('opacity', 1);
      }

      var html = '<div class="d">' + spec.dates[i] + '</div>';
      if (spec.kind === 'price') {
        var b = spec.bars[i];
        html += '<div class="row"><span>O</span><b>' + fmt(b[0]) + '</b></div>'
              + '<div class="row"><span>H</span><b>' + fmt(b[1]) + '</b></div>'
              + '<div class="row"><span>L</span><b>' + fmt(b[2]) + '</b></div>'
              + '<div class="row"><span>C</span><b>' + fmt(b[3]) + '</b></div>'
              + '<div class="row"><span>Объём</span><b>'
              + Math.round(b[4]).toLocaleString('ru-RU') + '</b></div>';
        dots.forEach(function (d) {
          var key = d.getAttribute('data-key');
          var v = spec.ma[key] && spec.ma[key][i];
          if (v === null || v === undefined) { d.setAttribute('opacity', 0); return; }
          d.setAttribute('cx', cx);
          d.setAttribute('cy', spec.yScale[0] + (v - spec.yDomain[0]) * spec.yScale[1]);
          d.setAttribute('opacity', 1);
        });
      } else {
        spec.series.forEach(function (s, k) {
          var v = s.values[i];
          if (v === null || v === undefined) return;
          html += '<div class="row"><span><i class="sw" style="background:'
                + s.color + '"></i>' + s.name + '</span><b>'
                + (v >= 0 ? '+' : '') + v.toFixed(1) + '%</b></div>';
          var d = dots[k];
          if (d) {
            d.setAttribute('cx', cx);
            d.setAttribute('cy', spec.yScale[0] + (v - spec.yDomain[0]) * spec.yScale[1]);
            d.setAttribute('opacity', 1);
          }
        });
      }
      tip.innerHTML = html;
      tip.style.opacity = 1;
      var px = cx / scale;
      var tw = tip.offsetWidth;
      px = Math.max(tw / 2 + 4, Math.min(px, r.width - tw / 2 - 4));
      tip.style.left = (px + box.scrollLeft) + 'px';
      tip.style.top = (spec.tipY / scale) + 'px';
    }

    svg.addEventListener('mousemove', move);
    svg.addEventListener('mouseleave', hide);
    hide();
  });
})();
"""

# Категориальные слоты 1,2,3,4,5,7 — светлые и тёмные шаги одной схемы.
SERIES_COLORS = [
    ("--s1", "#2a78d6", "#3987e5"),
    ("--s2", "#eb6834", "#d95926"),
    ("--s3", "#1baf7a", "#199e70"),
    ("--s4", "#eda100", "#c98500"),
    ("--s5", "#e87ba4", "#d55181"),
    ("--s6", "#4a3aa7", "#9085e9"),
]

# Скользящие средние — порядковая шкала одного тона: быстрая светлее, медленная темнее.
MA_COLORS = [
    ("--ma-fast", "#86b6ef", "#9ec5f4"),
    ("--ma-mid", "#2a78d6", "#5598e7"),
    ("--ma-slow", "#104281", "#256abf"),
]


def color_tokens() -> tuple[str, str]:
    """Возвращает (светлые, тёмные) объявления токенов графиков."""
    light = "\n".join(f"  {name}: {lo};" for name, lo, _ in SERIES_COLORS + MA_COLORS)
    dark = "\n".join(f"    {name}: {dk};" for name, _, dk in SERIES_COLORS + MA_COLORS)
    return light, dark


# ════════════════════════════════════════════════════════════════════════════
#  ШКАЛЫ
# ════════════════════════════════════════════════════════════════════════════

def nice_ticks(lo: float, hi: float, count: int = 5) -> list[float]:
    """Круглые значения внутри диапазона — для сетки и подписей оси."""
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / max(1, count)
    mag = 10 ** _floor_log10(raw)
    for mult in (1, 2, 2.5, 5, 10):
        step = mag * mult
        if step >= raw:
            break
    start = _ceil_div(lo, step) * step
    ticks: list[float] = []
    v = start
    while v <= hi + step * 1e-9 and len(ticks) < count + 2:
        ticks.append(round(v, 10))
        v += step
    return ticks


def _floor_log10(v: float) -> int:
    import math
    return int(math.floor(math.log10(v))) if v > 0 else 0


def _ceil_div(v: float, step: float) -> float:
    import math
    return math.ceil(v / step) if step else 0


def spread(ys: list[float], min_gap: float, top: float, bottom: float) -> list[float]:
    """Разводит подписи по вертикали, сохраняя их порядок и границы поля."""
    order = sorted(range(len(ys)), key=lambda i: ys[i])
    out = list(ys)
    for k in range(1, len(order)):
        cur, prev = order[k], order[k - 1]
        if out[cur] - out[prev] < min_gap:
            out[cur] = out[prev] + min_gap
    overflow = out[order[-1]] - bottom if order else 0.0
    if overflow > 0:
        for i in order:
            out[i] -= overflow
    if order and out[order[0]] < top:
        shift = top - out[order[0]]
        for i in order:
            out[i] += shift
    return out


def _fmt_tick(v: float) -> str:
    if abs(v) >= 1000:
        return f"{v:,.0f}".replace(",", " ")
    if abs(v) >= 10:
        return f"{v:.0f}"
    if abs(v) >= 1:
        return f"{v:.1f}"
    return f"{v:.3f}"


MONTHS_RU = ["янв", "фев", "мар", "апр", "май", "июн",
             "июл", "авг", "сен", "окт", "ноя", "дек"]


def month_marks(dates: Sequence[date]) -> list[tuple[int, str]]:
    """Индексы первых сессий каждого месяца — для оси времени."""
    marks: list[tuple[int, str]] = []
    prev: tuple[int, int] | None = None
    for i, d in enumerate(dates):
        key = (d.year, d.month)
        if key != prev:
            prev = key
            label = MONTHS_RU[d.month - 1]
            if d.month == 1 or not marks:
                label += f" {str(d.year)[2:]}"
            marks.append((i, label))
    return marks


# ════════════════════════════════════════════════════════════════════════════
#  ГРАФИК 1 — ДНЕВНЫЕ СВЕЧИ
# ════════════════════════════════════════════════════════════════════════════

def price_chart(snap, plan, fmt_level) -> str:
    hist = snap.hist
    if len(hist) < 10:
        return '<div class="empty">Недостаточно истории для графика.</div>'

    W, H = 1000.0, 452.0
    padL, padR, padT = 8.0, 96.0, 16.0
    price_h, vol_h, gap = 296.0, 66.0, 16.0
    plot_w = W - padL - padR
    price_top, vol_top = padT, padT + price_h + gap
    axis_y = vol_top + vol_h + 17.0

    dates = [row[0] for row in hist]
    opens = [row[1] for row in hist]
    highs = [row[2] for row in hist]
    lows = [row[3] for row in hist]
    closes = [row[4] for row in hist]
    vols = [row[5] for row in hist]
    n = len(hist)

    levels: list[tuple[str, float, str, str]] = []
    if plan is not None:
        levels = [
            ("stop", plan.stop_level, "Стоп", "var(--short)"),
            ("trigger", plan.trigger_level, "Триггер", "var(--brass)"),
            ("t1", plan.t1, "T1", "var(--long)"),
            ("t2", plan.t2, "T2", "var(--long)"),
        ]

    ma_series = [
        ("ema20", "20 EMA", "var(--ma-fast)"),
        ("sma50", "50 SMA", "var(--ma-mid)"),
        ("sma200", "200 SMA", "var(--ma-slow)"),
    ]

    pool = list(highs) + list(lows)
    for key, _, _ in ma_series:
        pool += [v for v in snap.ma_hist.get(key, []) if v is not None]
    pool += [lv for _, lv, _, _ in levels]
    lo, hi = min(pool), max(pool)
    pad = (hi - lo) * 0.05 or 1.0
    lo, hi = lo - pad, hi + pad
    span = hi - lo

    def y(v: float) -> float:
        return price_top + price_h * (1.0 - (v - lo) / span)

    step = plot_w / max(1, n - 1)
    body_w = max(1.6, min(9.0, step * 0.62))

    def x(i: int) -> float:
        return padL + i * step

    parts: list[str] = []

    # Сетка; цифры цены — внутри поля слева, правый край отдан уровням плана.
    # Текст осей дорисовывается последним, иначе свечи и средние его перекрывают.
    tick_text: list[str] = []
    for tick in nice_ticks(lo, hi, 5):
        ty = y(tick)
        parts.append(f'<line class="grid-line" x1="{padL:.1f}" y1="{ty:.1f}" '
                     f'x2="{padL + plot_w:.1f}" y2="{ty:.1f}"></line>')
        tick_text.append(f'<text class="axis-text" x="{padL + 5:.1f}" y="{ty - 4:.1f}">'
                         f'{_fmt_tick(tick)}</text>')

    # свечи
    for i in range(n):
        up = closes[i] >= opens[i]
        col = "var(--long)" if up else "var(--short)"
        cx = x(i)
        parts.append(f'<line x1="{cx:.2f}" y1="{y(highs[i]):.2f}" x2="{cx:.2f}" '
                     f'y2="{y(lows[i]):.2f}" stroke="{col}" stroke-width="1"></line>')
        top, bottom = y(max(opens[i], closes[i])), y(min(opens[i], closes[i]))
        parts.append(f'<rect x="{cx - body_w / 2:.2f}" y="{top:.2f}" width="{body_w:.2f}" '
                     f'height="{max(1.0, bottom - top):.2f}" fill="{col}"></rect>')

    # скользящие средние
    for key, _, col in ma_series:
        series = snap.ma_hist.get(key, [])
        pts = [f"{x(i):.2f},{y(v):.2f}" for i, v in enumerate(series)
               if v is not None and i < n]
        if len(pts) > 1:
            parts.append(f'<polyline class="ma-line" stroke="{col}" '
                         f'points="{" ".join(pts)}"></polyline>')

    # Уровни плана: линия по полю, подпись со значением в правом поле.
    if levels:
        label_ys = spread([y(v) for _, v, _, _ in levels], 12.0,
                          price_top + 6, price_top + price_h)
        for (_, value, label, col), ly_text in zip(levels, label_ys):
            ly = y(value)
            parts.append(f'<line class="lvl-line" stroke="{col}" x1="{padL:.1f}" y1="{ly:.1f}" '
                         f'x2="{padL + plot_w:.1f}" y2="{ly:.1f}"></line>')
            parts.append(f'<text class="lvl-text" fill="{col}" x="{padL + plot_w + 6:.1f}" '
                         f'y="{ly_text + 3.5:.1f}">{label} {fmt_level(value)}</text>')

    # объём
    vmax = max(vols) or 1.0
    for i in range(n):
        vh = vol_h * (vols[i] / vmax)
        col = "var(--long)" if closes[i] >= opens[i] else "var(--short)"
        parts.append(f'<rect x="{x(i) - body_w / 2:.2f}" y="{vol_top + vol_h - vh:.2f}" '
                     f'width="{body_w:.2f}" height="{vh:.2f}" fill="{col}" '
                     f'opacity=".38"></rect>')
    parts.append(f'<line class="axis-line" x1="{padL:.1f}" y1="{vol_top + vol_h:.1f}" '
                 f'x2="{padL + plot_w:.1f}" y2="{vol_top + vol_h:.1f}"></line>')
    parts.append(f'<text class="axis-text" x="{padL + plot_w + 6:.1f}" '
                 f'y="{vol_top + 10:.1f}">объём</text>')

    parts.extend(tick_text)

    # ось времени
    for i, label in month_marks(dates):
        if i == 0 and n > 6:
            continue
        parts.append(f'<text class="axis-text" x="{x(i):.1f}" y="{axis_y:.1f}" '
                     f'text-anchor="middle">{label}</text>')

    # слой наведения
    parts.append(f'<line class="crosshair" x1="0" y1="{price_top:.1f}" x2="0" '
                 f'y2="{vol_top + vol_h:.1f}" opacity="0"></line>')
    for key, _, col in ma_series:
        parts.append(f'<circle class="hover-dot" data-key="{key}" r="3.2" fill="{col}" '
                     f'opacity="0"></circle>')

    spec = {
        "kind": "price", "vw": W, "x0": padL, "step": step, "n": n,
        "yDomain": [lo, hi], "yScale": [price_top + price_h, -price_h / span],
        "tipY": price_top + 8,
        "dates": [d.isoformat() for d in dates],
        "bars": [[opens[i], highs[i], lows[i], closes[i], vols[i]] for i in range(n)],
        "ma": {key: snap.ma_hist.get(key, []) for key, _, _ in ma_series},
    }
    return _wrap_svg(W, H, parts, spec)


# ════════════════════════════════════════════════════════════════════════════
#  ГРАФИК 2 — ОТНОСИТЕЛЬНАЯ ДИНАМИКА
# ════════════════════════════════════════════════════════════════════════════

def performance_chart(snaps: list, limit: int = 6) -> str:
    """Динамика в процентах от начала окна — общая база для сравнения."""
    usable = [s for s in snaps if len(s.hist) >= 10][:limit]
    if len(usable) < 2:
        return ('<div class="empty">Для сравнения нужно минимум два тикера '
                'с достаточной историей.</div>')

    common = set(d for d, *_ in usable[0].hist)
    for s in usable[1:]:
        common &= set(d for d, *_ in s.hist)
    dates = sorted(common)
    if len(dates) < 10:
        return ('<div class="empty">У тикеров нет общего окна истории — '
                'сравнивать нечего.</div>')

    W, H = 1000.0, 356.0
    padL, padR, padT, padB = 8.0, 96.0, 16.0, 30.0
    plot_w, plot_h = W - padL - padR, H - padT - padB

    series: list[dict] = []
    for idx, s in enumerate(usable):
        by_date = {d: c for d, _o, _h, _l, c, _v in s.hist}
        base = by_date[dates[0]]
        values = [((by_date[d] - base) / base * 100.0) if base else 0.0 for d in dates]
        token = SERIES_COLORS[idx % len(SERIES_COLORS)][0]
        series.append({"name": s.symbol, "values": values, "color": f"var({token})"})

    flat = [v for s in series for v in s["values"]]
    lo, hi = min(flat + [0.0]), max(flat + [0.0])
    pad = (hi - lo) * 0.08 or 1.0
    lo, hi = lo - pad, hi + pad
    span = hi - lo
    n = len(dates)
    step = plot_w / max(1, n - 1)

    def x(i: int) -> float:
        return padL + i * step

    def y(v: float) -> float:
        return padT + plot_h * (1.0 - (v - lo) / span)

    parts: list[str] = []
    # Проценты — внутри поля слева: правый край занят подписями тикеров.
    tick_text: list[str] = []
    for tick in nice_ticks(lo, hi, 5):
        ty = y(tick)
        parts.append(f'<line class="grid-line" x1="{padL:.1f}" y1="{ty:.1f}" '
                     f'x2="{padL + plot_w:.1f}" y2="{ty:.1f}"></line>')
        tick_text.append(f'<text class="axis-text" x="{padL + 5:.1f}" '
                         f'y="{ty - 4:.1f}">{tick:+.0f}%</text>')

    if lo < 0 < hi:                                  # база сравнения
        parts.append(f'<line class="base-line" x1="{padL:.1f}" y1="{y(0):.1f}" '
                     f'x2="{padL + plot_w:.1f}" y2="{y(0):.1f}"></line>')

    for s in series:
        pts = " ".join(f"{x(i):.2f},{y(v):.2f}" for i, v in enumerate(s["values"]))
        parts.append(f'<polyline class="series-line" stroke="{s["color"]}" '
                     f'points="{pts}"></polyline>')

    # Прямые подписи справа — обязательны: они несут идентичность помимо цвета.
    label_ys = spread([y(s["values"][-1]) for s in series], 14.0, padT + 6, padT + plot_h)
    for s, ly in zip(series, label_ys):
        parts.append(f'<text class="series-label" fill="{s["color"]}" '
                     f'x="{padL + plot_w + 6:.1f}" y="{ly + 3.5:.1f}">'
                     f'{s["name"]} {s["values"][-1]:+.1f}%</text>')

    parts.extend(tick_text)

    for i, label in month_marks(dates):
        if i == 0 and n > 6:
            continue
        parts.append(f'<text class="axis-text" x="{x(i):.1f}" y="{padT + plot_h + 18:.1f}" '
                     f'text-anchor="middle">{label}</text>')

    parts.append(f'<line class="crosshair" x1="0" y1="{padT:.1f}" x2="0" '
                 f'y2="{padT + plot_h:.1f}" opacity="0"></line>')
    for s in series:
        parts.append(f'<circle class="hover-dot" r="3.2" fill="{s["color"]}" '
                     f'opacity="0"></circle>')

    spec = {
        "kind": "perf", "vw": W, "x0": padL, "step": step, "n": n,
        "yDomain": [lo, hi], "yScale": [padT + plot_h, -plot_h / span],
        "tipY": padT + 8,
        "dates": [d.isoformat() for d in dates],
        "series": [{"name": s["name"], "values": [round(v, 2) for v in s["values"]],
                    "color": s["color"]} for s in series],
    }
    return _wrap_svg(W, H, parts, spec)


def _wrap_svg(w: float, h: float, parts: list[str], spec: dict) -> str:
    data = json.dumps(spec, separators=(",", ":")).replace('"', "&quot;")
    return (f'<div class="chart-scroll"><svg class="chart-svg" viewBox="0 0 {w:.0f} {h:.0f}" '
            f'preserveAspectRatio="xMidYMid meet" role="img" data-chart="{data}">'
            + "".join(parts) + '</svg></div>')
