"""Живые графики брифа: свечи и относительная динамика.

В отличие от статичной картинки, отрисовка идёт в браузере: Python отдаёт
историю и параметры, движок рисует SVG и перерисовывает его на каждый жест.
Отсюда зум колесом, перетаскивание, перекрестие с подписями на осях и строка
OHLC в шапке, которая идёт за курсором — как на торговом терминале.

Размер полотна берётся в реальных пикселях и пересчитывается при изменении
ширины окна, поэтому подписи осей не мельчают на узком экране.

Палитра сравнения — категориальные слоты 1,2,3,4,5,7 стандартной схемы.
Слоты 6 (зелёный) и 8 (красный) намеренно пропущены: на этой странице
зелёный и красный уже заняты семантикой направления (лонг/шорт, рост/падение),
и линия тикера такого же цвета читалась бы как оценка, а не как идентификатор.
Набор проверен валидатором в обеих темах против поверхностей страницы.
"""

from __future__ import annotations

import json

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
#  СТИЛИ
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
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.reset-zoom {
  font: inherit;
  font-family: "IBM Plex Mono", monospace;
  font-size: 11px;
  color: var(--ink-soft);
  background: var(--surface-sunk);
  border: 1px solid var(--rule);
  border-radius: 3px;
  padding: 3px 9px;
  cursor: pointer;
  opacity: 0;
  pointer-events: none;          /* спрятанная кнопка не должна ловить клики */
  transition: opacity .15s;
}
.reset-zoom.on { opacity: 1; pointer-events: auto; }
.reset-zoom:hover { border-color: var(--brass); color: var(--brass); }
.reset-zoom:focus-visible { outline: 2px solid var(--brass); outline-offset: 2px; }

/* Строка под курсором — как легенда терминала: значения идут за перекрестием. */
.readout {
  display: flex;
  flex-wrap: wrap;
  gap: 3px 14px;
  padding: 9px 18px;
  border-bottom: 1px solid var(--rule-soft);
  font-family: "IBM Plex Mono", monospace;
  font-variant-numeric: tabular-nums;
  font-size: 11.5px;
  color: var(--ink-soft);
  min-height: 34px;
  align-items: center;
}
.readout .k { color: var(--ink-faint); margin-right: 5px; }
.readout b { color: var(--ink); font-weight: 600; }
.readout .sw {
  display: inline-block; width: 9px; height: 3px; border-radius: 1px;
  margin-right: 6px; vertical-align: 3px;
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
  width: 14px; height: 2px;
  border-radius: 1px;
  margin-right: 6px;
  vertical-align: 3px;
}
.legend .key { white-space: nowrap; }
.legend .dash { height: 0; border-top: 2px dashed currentColor; background: none; }

.chart-box {
  position: relative;
  min-width: 0;
  overflow: hidden;
  touch-action: pan-y;
  cursor: crosshair;
  user-select: none;
  -webkit-user-select: none;
}
.chart-box.grabbing { cursor: grabbing; }
.chart-box svg { display: block; }
.chart-hint {
  padding: 8px 18px 12px;
  font-family: "IBM Plex Mono", monospace;
  font-size: 10.5px;
  color: var(--ink-faint);
}

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
.ma-line { fill: none; stroke-width: 1.6; stroke-linejoin: round; stroke-linecap: round; }
.series-line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.series-label {
  font-family: "IBM Plex Mono", monospace;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}
.base-line { stroke: var(--ink-faint); stroke-width: 1; stroke-dasharray: 3 3; }

.cross-line { stroke: var(--ink-faint); stroke-width: 1; stroke-dasharray: 3 3; }
.cross-tagbg { fill: var(--ink-soft); }
.cross-tagtext {
  fill: var(--surface);
  font-family: "IBM Plex Mono", monospace;
  font-size: 10px;
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}
.cross-dot { stroke: var(--surface); stroke-width: 1.5; }
"""


# ════════════════════════════════════════════════════════════════════════════
#  ДВИЖОК ОТРИСОВКИ
# ════════════════════════════════════════════════════════════════════════════

CHART_JS = r"""
(function () {
  var MIN_BARS = 20;

  function fmtNum(v, digits) {
    if (v === null || v === undefined || !isFinite(v)) return '—';
    var a = Math.abs(v);
    var d = digits !== undefined ? digits : (a >= 1000 ? 2 : (a >= 1 ? 2 : 4));
    return v.toLocaleString('ru-RU', {minimumFractionDigits: d, maximumFractionDigits: d});
  }

  function niceTicks(lo, hi, count) {
    if (!(hi > lo)) return [lo];
    var raw = (hi - lo) / count;
    var mag = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
    var step = mag;
    var mults = [1, 2, 2.5, 5, 10];
    for (var i = 0; i < mults.length; i++) {
      step = mag * mults[i];
      if (step >= raw) break;
    }
    var out = [];
    var v = Math.ceil(lo / step) * step;
    while (v <= hi + step * 1e-9 && out.length < count + 3) {
      out.push(Math.round(v * 1e10) / 1e10);
      v += step;
    }
    return out;
  }

  var MONTHS = ['янв','фев','мар','апр','май','июн','июл','авг','сен','окт','ноя','дек'];
  function monthLabel(iso, withYear) {
    var p = iso.split('-');
    return MONTHS[parseInt(p[1], 10) - 1] + (withYear ? ' ' + p[0].slice(2) : '');
  }

  // Разводит подписи по вертикали, сохраняя порядок и границы поля.
  function spread(ys, gap, top, bottom) {
    var order = ys.map(function (y, i) { return i; })
                  .sort(function (a, b) { return ys[a] - ys[b]; });
    var out = ys.slice();
    for (var k = 1; k < order.length; k++) {
      if (out[order[k]] - out[order[k - 1]] < gap) out[order[k]] = out[order[k - 1]] + gap;
    }
    if (order.length) {
      var over = out[order[order.length - 1]] - bottom;
      if (over > 0) for (var i = 0; i < order.length; i++) out[order[i]] -= over;
      if (out[order[0]] < top) {
        var sh = top - out[order[0]];
        for (var j = 0; j < order.length; j++) out[order[j]] += sh;
      }
    }
    return out;
  }

  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function init(box) {
    var spec;
    try { spec = JSON.parse(box.getAttribute('data-chart')); } catch (e) { return; }
    var total = spec.dates.length;
    if (total < 2) return;

    var card = box.closest('.chart-card');
    var readout = card ? card.querySelector('.readout') : null;
    var resetBtn = card ? card.querySelector('.reset-zoom') : null;

    var view = {
      from: Math.max(0, total - spec.viewBars),
      to: total
    };
    var home = {from: view.from, to: view.to};
    var hover = null;
    var geom = null;

    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    box.appendChild(svg);
    var layers = {};
    ['grid', 'marks', 'axes', 'cross'].forEach(function (name) {
      var g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
      g.setAttribute('class', 'layer-' + name);
      svg.appendChild(g);
      layers[name] = g;
    });

    function isHome() {
      return view.from === home.from && view.to === home.to;
    }

    function draw() {
      // Полотно ровно по контейнеру: подписи рисуются в реальных пикселях,
      // поэтому на узком экране они не мельчают, а страница не едет вбок.
      var W = Math.max(280, box.clientWidth || 900);
      var H = spec.height;
      svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
      svg.setAttribute('width', W);
      svg.setAttribute('height', H);

      var padL = 8, padT = 14;
      var padR = Math.min(spec.padR, Math.max(52, W * 0.19));
      var terse = padR < spec.padR - 4;      // узко: подписи уровней без названия
      var axisH = 26;
      var volH = spec.kind === 'price' ? 62 : 0;
      var volGap = volH ? 14 : 0;
      var plotW = W - padL - padR;
      var plotH = H - padT - axisH - volH - volGap;
      var volTop = padT + plotH + volGap;

      var from = view.from, to = view.to, n = to - from;
      var step = plotW / Math.max(1, n - 1);
      var bodyW = Math.max(1.2, Math.min(11, step * 0.66));

      // ── шкала цены по видимому окну ───────────────────────────────────
      var lo = Infinity, hi = -Infinity, i, v;
      if (spec.kind === 'price') {
        for (i = from; i < to; i++) {
          if (spec.l[i] < lo) lo = spec.l[i];
          if (spec.h[i] > hi) hi = spec.h[i];
        }
        Object.keys(spec.ma).forEach(function (k) {
          for (var j = from; j < to; j++) {
            v = spec.ma[k][j];
            if (v === null) continue;
            if (v < lo) lo = v;
            if (v > hi) hi = v;
          }
        });
        // Уровни плана держим в кадре, только когда виден правый край истории.
        if (to >= total && spec.levels.length) {
          spec.levels.forEach(function (L) {
            if (L.value < lo) lo = L.value;
            if (L.value > hi) hi = L.value;
          });
        }
      } else {
        var base = {};
        spec.series.forEach(function (s) { base[s.name] = s.values[from]; });
        spec.series.forEach(function (s) {
          for (var j = from; j < to; j++) {
            v = rel(s, j, base[s.name]);
            if (v === null) continue;
            if (v < lo) lo = v;
            if (v > hi) hi = v;
          }
        });
        if (lo > 0) lo = 0;
        if (hi < 0) hi = 0;
      }
      if (!isFinite(lo) || !isFinite(hi)) return;
      var pad = (hi - lo) * 0.06 || 1;
      lo -= pad; hi += pad;
      var span = hi - lo;

      function X(idx) { return padL + (idx - from) * step; }
      function Y(val) { return padT + plotH * (1 - (val - lo) / span); }

      geom = {W: W, H: H, padL: padL, padR: padR, padT: padT, plotW: plotW, plotH: plotH,
              step: step, from: from, to: to, lo: lo, hi: hi, span: span,
              volTop: volTop, volH: volH, X: X, Y: Y};

      // ── сетка ─────────────────────────────────────────────────────────
      var grid = [], axes = [], marks = [];
      var ticks = niceTicks(lo, hi, 5);
      ticks.forEach(function (t) {
        var ty = Y(t);
        grid.push('<line class="grid-line" x1="' + padL + '" y1="' + ty.toFixed(1) +
                  '" x2="' + (padL + plotW) + '" y2="' + ty.toFixed(1) + '"></line>');
        axes.push('<text class="axis-text" x="' + (padL + 5) + '" y="' + (ty - 4).toFixed(1) +
                  '">' + (spec.kind === 'price' ? fmtNum(t, t >= 100 ? 0 : 2)
                                                : (t > 0 ? '+' : '') + t.toFixed(0) + '%') +
                  '</text>');
      });

      // ── метки времени ─────────────────────────────────────────────────
      var prevMonth = null, marksCount = 0;
      var minGapPx = 54;
      var lastLabelX = -1e9;
      for (i = from; i < to; i++) {
        var mo = spec.dates[i].slice(0, 7);
        if (mo === prevMonth) continue;
        prevMonth = mo;
        if (i === from && n > 6) continue;
        var lx = X(i);
        if (lx - lastLabelX < minGapPx) continue;
        lastLabelX = lx;
        marksCount++;
        axes.push('<text class="axis-text" x="' + lx.toFixed(1) + '" y="' + (H - 8) +
                  '" text-anchor="middle">' +
                  monthLabel(spec.dates[i], spec.dates[i].slice(5, 7) === '01' || marksCount === 1) +
                  '</text>');
      }

      // ── марки ─────────────────────────────────────────────────────────
      if (spec.kind === 'price') {
        var vmax = 0;
        for (i = from; i < to; i++) if (spec.v[i] > vmax) vmax = spec.v[i];
        vmax = vmax || 1;
        for (i = from; i < to; i++) {
          var up = spec.c[i] >= spec.o[i];
          var col = up ? 'var(--long)' : 'var(--short)';
          var cx = X(i);
          marks.push('<line x1="' + cx.toFixed(2) + '" y1="' + Y(spec.h[i]).toFixed(2) +
                     '" x2="' + cx.toFixed(2) + '" y2="' + Y(spec.l[i]).toFixed(2) +
                     '" stroke="' + col + '" stroke-width="1"></line>');
          var top = Y(Math.max(spec.o[i], spec.c[i]));
          var bot = Y(Math.min(spec.o[i], spec.c[i]));
          marks.push('<rect x="' + (cx - bodyW / 2).toFixed(2) + '" y="' + top.toFixed(2) +
                     '" width="' + bodyW.toFixed(2) + '" height="' +
                     Math.max(1, bot - top).toFixed(2) + '" fill="' + col + '"></rect>');
          var vh = volH * (spec.v[i] / vmax);
          marks.push('<rect x="' + (cx - bodyW / 2).toFixed(2) + '" y="' +
                     (volTop + volH - vh).toFixed(2) + '" width="' + bodyW.toFixed(2) +
                     '" height="' + vh.toFixed(2) + '" fill="' + col + '" opacity=".34"></rect>');
        }
        Object.keys(spec.ma).forEach(function (k) {
          var pts = [], series = spec.ma[k];
          for (var j = from; j < to; j++) {
            if (series[j] === null) continue;
            pts.push(X(j).toFixed(2) + ',' + Y(series[j]).toFixed(2));
          }
          if (pts.length > 1) {
            marks.push('<polyline class="ma-line" stroke="var(' + spec.maColors[k] +
                       ')" points="' + pts.join(' ') + '"></polyline>');
          }
        });
        marks.push('<line class="axis-line" x1="' + padL + '" y1="' + (volTop + volH) +
                   '" x2="' + (padL + plotW) + '" y2="' + (volTop + volH) + '"></line>');
        axes.push('<text class="axis-text" x="' + (padL + plotW + 6) + '" y="' +
                  (volTop + 10) + '">объём</text>');

        var shown = spec.levels.filter(function (L) {
          return L.value >= lo && L.value <= hi;
        });
        if (shown.length) {
          var lys = spread(shown.map(function (L) { return Y(L.value); }), 12,
                           padT + 6, padT + plotH);
          shown.forEach(function (L, k) {
            var ly = Y(L.value);
            marks.push('<line class="lvl-line" stroke="' + L.color + '" x1="' + padL +
                       '" y1="' + ly.toFixed(1) + '" x2="' + (padL + plotW) + '" y2="' +
                       ly.toFixed(1) + '"></line>');
            axes.push('<text class="lvl-text" fill="' + L.color + '" x="' +
                      (padL + plotW + 6) + '" y="' + (lys[k] + 3.5).toFixed(1) + '">' +
                      (terse ? '' : esc(L.name) + ' ') + fmtNum(L.value) + '</text>');
          });
        }
      } else {
        var b0 = {};
        spec.series.forEach(function (s) { b0[s.name] = s.values[from]; });
        if (lo < 0 && hi > 0) {
          marks.push('<line class="base-line" x1="' + padL + '" y1="' + Y(0).toFixed(1) +
                     '" x2="' + (padL + plotW) + '" y2="' + Y(0).toFixed(1) + '"></line>');
        }
        var ends = [];
        spec.series.forEach(function (s) {
          var pts = [];
          for (var j = from; j < to; j++) {
            var val = rel(s, j, b0[s.name]);
            if (val === null) continue;
            pts.push(X(j).toFixed(2) + ',' + Y(val).toFixed(2));
          }
          if (pts.length > 1) {
            marks.push('<polyline class="series-line" stroke="' + s.color +
                       '" points="' + pts.join(' ') + '"></polyline>');
          }
          ends.push({s: s, val: rel(s, to - 1, b0[s.name])});
        });
        var eys = spread(ends.map(function (e) { return Y(e.val); }), 14,
                         padT + 6, padT + plotH);
        ends.forEach(function (e, k) {
          axes.push('<text class="series-label" fill="' + e.s.color + '" x="' +
                    (padL + plotW + 6) + '" y="' + (eys[k] + 3.5).toFixed(1) + '">' +
                    esc(e.s.name) + (terse ? '' :
                      ' ' + (e.val >= 0 ? '+' : '') + e.val.toFixed(1) + '%') + '</text>');
        });
      }

      layers.grid.innerHTML = grid.join('');
      layers.marks.innerHTML = marks.join('');
      layers.axes.innerHTML = axes.join('');
      if (resetBtn) resetBtn.classList.toggle('on', !isHome());
      drawCross();
    }

    function rel(s, idx, base) {
      var v = s.values[idx];
      if (v === null || v === undefined || !base) return null;
      return (v - base) / base * 100;
    }

    // ── перекрестие и строка значений ────────────────────────────────────
    function drawCross() {
      if (!geom) return;
      if (hover === null) {
        layers.cross.innerHTML = '';
        fillReadout(view.to - 1, false);
        return;
      }
      var i = hover;
      var cx = geom.X(i);
      var out = ['<line class="cross-line" x1="' + cx.toFixed(1) + '" y1="' + geom.padT +
                 '" x2="' + cx.toFixed(1) + '" y2="' +
                 (geom.volTop + geom.volH || geom.padT + geom.plotH).toFixed(1) + '"></line>'];

      var label = spec.dates[i];
      var tw = label.length * 5.6 + 12;
      var tx = Math.max(geom.padL, Math.min(cx - tw / 2, geom.padL + geom.plotW - tw));
      out.push('<rect class="cross-tagbg" x="' + tx.toFixed(1) + '" y="' + (geom.H - 20) +
               '" width="' + tw.toFixed(1) + '" height="15" rx="2"></rect>');
      out.push('<text class="cross-tagtext" x="' + (tx + tw / 2).toFixed(1) + '" y="' +
               (geom.H - 9) + '" text-anchor="middle">' + label + '</text>');

      if (spec.kind === 'price') {
        var py = geom.Y(spec.c[i]);
        out.push(hLine(py));
        out.push(priceTag(py, fmtNum(spec.c[i])));
        Object.keys(spec.ma).forEach(function (k) {
          var v = spec.ma[k][i];
          if (v === null) return;
          out.push('<circle class="cross-dot" cx="' + cx.toFixed(1) + '" cy="' +
                   geom.Y(v).toFixed(1) + '" r="3" fill="var(' + spec.maColors[k] + ')"></circle>');
        });
      } else {
        var b0 = {};
        spec.series.forEach(function (s) { b0[s.name] = s.values[geom.from]; });
        spec.series.forEach(function (s) {
          var v = rel(s, i, b0[s.name]);
          if (v === null) return;
          out.push('<circle class="cross-dot" cx="' + cx.toFixed(1) + '" cy="' +
                   geom.Y(v).toFixed(1) + '" r="3" fill="' + s.color + '"></circle>');
        });
      }
      layers.cross.innerHTML = out.join('');
      fillReadout(i, true);
    }

    function hLine(py) {
      return '<line class="cross-line" x1="' + geom.padL + '" y1="' + py.toFixed(1) +
             '" x2="' + (geom.padL + geom.plotW) + '" y2="' + py.toFixed(1) + '"></line>';
    }

    function priceTag(py, text) {
      var w = text.length * 5.4 + 12;
      var x = geom.padL + geom.plotW + 2;
      return '<rect class="cross-tagbg" x="' + x + '" y="' + (py - 7.5).toFixed(1) +
             '" width="' + w.toFixed(1) + '" height="15" rx="2"></rect>' +
             '<text class="cross-tagtext" x="' + (x + w / 2).toFixed(1) + '" y="' +
             (py + 3.5).toFixed(1) + '" text-anchor="middle">' + text + '</text>';
    }

    function fillReadout(i, live) {
      if (!readout || i < 0 || i >= total) return;
      var html = '<span class="k">' + spec.dates[i] + '</span>';
      if (spec.kind === 'price') {
        var chg = i > 0 ? (spec.c[i] - spec.c[i - 1]) / spec.c[i - 1] * 100 : 0;
        html += '<span><span class="k">O</span><b>' + fmtNum(spec.o[i]) + '</b></span>' +
                '<span><span class="k">H</span><b>' + fmtNum(spec.h[i]) + '</b></span>' +
                '<span><span class="k">L</span><b>' + fmtNum(spec.l[i]) + '</b></span>' +
                '<span><span class="k">C</span><b>' + fmtNum(spec.c[i]) + '</b></span>' +
                '<span class="' + (chg >= 0 ? 'up' : 'down') + '">' +
                (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%</span>' +
                '<span><span class="k">объём</span><b>' +
                Math.round(spec.v[i]).toLocaleString('ru-RU') + '</b></span>';
      } else {
        var b0 = {};
        spec.series.forEach(function (s) { b0[s.name] = s.values[view.from]; });
        spec.series.forEach(function (s) {
          var v = rel(s, i, b0[s.name]);
          if (v === null) return;
          html += '<span><i class="sw" style="background:' + s.color + '"></i>' +
                  '<span class="k">' + esc(s.name) + '</span><b>' +
                  (v >= 0 ? '+' : '') + v.toFixed(1) + '%</b></span>';
        });
      }
      readout.innerHTML = html;
      readout.style.opacity = live ? 1 : .72;
    }

    // ── жесты ────────────────────────────────────────────────────────────
    function indexAt(clientX) {
      if (!geom) return null;
      var r = svg.getBoundingClientRect();
      if (!r.width) return null;
      var x = (clientX - r.left) * (geom.W / r.width);
      var i = Math.round(view.from + (x - geom.padL) / geom.step);
      return Math.max(view.from, Math.min(view.to - 1, i));
    }

    function setView(from, to) {
      from = Math.round(from); to = Math.round(to);
      if (to - from < MIN_BARS) {
        var mid = (from + to) / 2;
        from = Math.round(mid - MIN_BARS / 2);
        to = from + MIN_BARS;
      }
      if (from < 0) { to -= from; from = 0; }
      if (to > total) { from -= (to - total); to = total; }
      if (from < 0) from = 0;
      if (to - from < 2) return;
      view.from = from; view.to = to;
      draw();
    }

    box.addEventListener('wheel', function (ev) {
      ev.preventDefault();
      var i = indexAt(ev.clientX);
      if (i === null) return;
      var n = view.to - view.from;
      var factor = ev.deltaY > 0 ? 1.15 : 1 / 1.15;
      var next = Math.max(MIN_BARS, Math.min(total, Math.round(n * factor)));
      var anchor = (i - view.from) / Math.max(1, n - 1);
      var from = i - anchor * (next - 1);
      setView(from, from + next);
      hover = indexAt(ev.clientX);
      drawCross();
    }, {passive: false});

    var drag = null;
    box.addEventListener('mousedown', function (ev) {
      drag = {x: ev.clientX, from: view.from, to: view.to};
      box.classList.add('grabbing');
    });
    window.addEventListener('mouseup', function () {
      drag = null;
      box.classList.remove('grabbing');
    });
    box.addEventListener('mousemove', function (ev) {
      if (drag && geom) {
        var r = svg.getBoundingClientRect();
        var dx = (ev.clientX - drag.x) * (geom.W / r.width);
        var shift = Math.round(dx / geom.step);
        if (shift !== 0) setView(drag.from - shift, drag.to - shift);
      }
      hover = indexAt(ev.clientX);
      drawCross();
    });
    box.addEventListener('mouseleave', function () {
      hover = null;
      drawCross();
    });

    // Касания: один палец тянет, два — щипок для зума.
    var touch = null;
    box.addEventListener('touchstart', function (ev) {
      if (ev.touches.length === 1) {
        touch = {mode: 'pan', x: ev.touches[0].clientX, from: view.from, to: view.to};
      } else if (ev.touches.length === 2) {
        touch = {mode: 'zoom', from: view.from, to: view.to,
                 dist: Math.abs(ev.touches[0].clientX - ev.touches[1].clientX) || 1};
      }
    }, {passive: true});
    box.addEventListener('touchmove', function (ev) {
      if (!touch || !geom) return;
      var r = svg.getBoundingClientRect();
      if (touch.mode === 'pan' && ev.touches.length === 1) {
        var dx = (ev.touches[0].clientX - touch.x) * (geom.W / r.width);
        var shift = Math.round(dx / geom.step);
        if (shift !== 0) { ev.preventDefault(); setView(touch.from - shift, touch.to - shift); }
      } else if (touch.mode === 'zoom' && ev.touches.length === 2) {
        ev.preventDefault();
        var d = Math.abs(ev.touches[0].clientX - ev.touches[1].clientX) || 1;
        var n = touch.to - touch.from;
        var next = Math.max(MIN_BARS, Math.min(total, Math.round(n * touch.dist / d)));
        var mid = (touch.from + touch.to) / 2;
        setView(mid - next / 2, mid + next / 2);
      }
    }, {passive: false});
    box.addEventListener('touchend', function () { touch = null; });

    box.addEventListener('dblclick', function () { setView(home.from, home.to); });
    if (resetBtn) {
      resetBtn.addEventListener('click', function () { setView(home.from, home.to); });
    }

    var pending = null;
    var ro = new ResizeObserver(function () {
      if (pending) cancelAnimationFrame(pending);
      pending = requestAnimationFrame(draw);
    });
    ro.observe(box);

    draw();
  }

  document.querySelectorAll('.chart-box[data-chart]').forEach(init);
})();
"""


# ════════════════════════════════════════════════════════════════════════════
#  СБОРКА ДАННЫХ ДЛЯ БРАУЗЕРА
# ════════════════════════════════════════════════════════════════════════════

def _round(values, digits: int = 4):
    return [None if v is None else round(float(v), digits) for v in values]


def _embed(spec: dict) -> str:
    data = json.dumps(spec, separators=(",", ":"), ensure_ascii=False).replace('"', "&quot;")
    return f'<div class="chart-box" data-chart="{data}"></div>'


def price_chart(snap, plan, view_bars: int = 130) -> str:
    """Свечи, средние, объём и уровни плана. Рисуется движком в браузере."""
    hist = snap.hist
    if len(hist) < 10:
        return '<div class="empty">Недостаточно истории для графика.</div>'

    levels = []
    if plan is not None:
        for name, value, color in (("Стоп", plan.stop_level, "var(--short)"),
                                   ("Триггер", plan.trigger_level, "var(--brass)"),
                                   ("T1", plan.t1, "var(--long)"),
                                   ("T2", plan.t2, "var(--long)")):
            levels.append({"name": name, "value": round(float(value), 4), "color": color})

    spec = {
        "kind": "price",
        "height": 430,
        "padR": 96,
        "viewBars": view_bars,
        "dates": [row[0].isoformat() for row in hist],
        "o": _round([row[1] for row in hist]),
        "h": _round([row[2] for row in hist]),
        "l": _round([row[3] for row in hist]),
        "c": _round([row[4] for row in hist]),
        "v": [int(row[5]) for row in hist],
        "ma": {key: _round(snap.ma_hist.get(key, [])) for key in ("ema20", "sma50", "sma200")},
        "maColors": {"ema20": "--ma-fast", "sma50": "--ma-mid", "sma200": "--ma-slow"},
        "levels": levels,
    }
    return _embed(spec)


def performance_chart(snaps: list, limit: int = 6, view_bars: int = 130) -> str:
    """Динамика в процентах от левого края видимого окна — база едет за зумом."""
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

    series = []
    for idx, s in enumerate(usable):
        by_date = {d: c for d, _o, _h, _l, c, _v in s.hist}
        token = SERIES_COLORS[idx % len(SERIES_COLORS)][0]
        series.append({"name": s.symbol,
                       "values": _round([by_date[d] for d in dates]),
                       "color": f"var({token})"})

    spec = {
        "kind": "perf",
        "height": 340,
        "padR": 104,
        "viewBars": view_bars,
        "dates": [d.isoformat() for d in dates],
        "series": series,
    }
    return _embed(spec)
