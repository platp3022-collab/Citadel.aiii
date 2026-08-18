"""HTML-рендер брифа: самодостаточная страница, открывается в браузере.

Никаких внешних файлов и скриптов, кроме шрифтов Google Fonts (с честным
запасным стеком). Стили — токенами, поэтому страница читается и в светлой,
и в тёмной теме системы, и по кнопке переключения.
"""

from __future__ import annotations

import html
from datetime import date, datetime
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:                                  # только для подсказок типов
    from market_dashboard import Assessment, Events, Snapshot

# ════════════════════════════════════════════════════════════════════════════
#  СТИЛИ
# ════════════════════════════════════════════════════════════════════════════

CSS = """
:root {
  --ground: #eef0ee;
  --surface: #ffffff;
  --surface-sunk: #f7f8f7;
  --ink: #15181a;
  --ink-soft: #4a5157;
  --ink-faint: #7d868d;
  --rule: #d8dcd9;
  --rule-soft: #e6e9e6;
  --brass: #9a6f2c;
  --brass-soft: #f0e6d4;
  --long: #1a6b46;
  --long-soft: #dfeee6;
  --short: #a3372f;
  --short-soft: #f7e2e0;
  --warn: #9c6a12;
  --warn-soft: #f7ecd6;
  --shadow: 0 1px 2px rgba(16, 20, 24, .06), 0 8px 24px -16px rgba(16, 20, 24, .28);
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground: #0e1114;
    --surface: #161a1e;
    --surface-sunk: #1c2126;
    --ink: #e6e9ea;
    --ink-soft: #a8b1b7;
    --ink-faint: #79838a;
    --rule: #2a3138;
    --rule-soft: #222930;
    --brass: #cfa055;
    --brass-soft: #2e2618;
    --long: #4cbb87;
    --long-soft: #163024;
    --short: #e0776c;
    --short-soft: #331d1b;
    --warn: #d9a445;
    --warn-soft: #2f2616;
    --shadow: 0 1px 2px rgba(0, 0, 0, .4), 0 8px 24px -16px rgba(0, 0, 0, .8);
  }
}

:root[data-theme="dark"] {
  --ground: #0e1114;
  --surface: #161a1e;
  --surface-sunk: #1c2126;
  --ink: #e6e9ea;
  --ink-soft: #a8b1b7;
  --ink-faint: #79838a;
  --rule: #2a3138;
  --rule-soft: #222930;
  --brass: #cfa055;
  --brass-soft: #2e2618;
  --long: #4cbb87;
  --long-soft: #163024;
  --short: #e0776c;
  --short-soft: #331d1b;
  --warn: #d9a445;
  --warn-soft: #2f2616;
  --shadow: 0 1px 2px rgba(0, 0, 0, .4), 0 8px 24px -16px rgba(0, 0, 0, .8);
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--ground);
  color: var(--ink);
  font-family: "IBM Plex Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 15px;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}

.wrap {
  max-width: 1180px;
  margin: 0 auto;
  padding: 0 20px 72px;
  display: flex;
  flex-direction: column;
  gap: 40px;
}

/* ── шапка ────────────────────────────────────────────────────────────── */

.masthead {
  border-bottom: 2px solid var(--brass);
  padding: 32px 0 14px;
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px 24px;
}

.masthead h1 {
  font-family: Archivo, "IBM Plex Sans", sans-serif;
  font-size: clamp(24px, 3.4vw, 36px);
  font-weight: 700;
  letter-spacing: -.022em;
  line-height: 1.05;
  margin: 0;
  text-wrap: balance;
}

.masthead .date {
  display: block;
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 13px;
  letter-spacing: .06em;
  color: var(--brass);
  margin-bottom: 6px;
}

.meta {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 11.5px;
  color: var(--ink-faint);
  text-align: right;
  line-height: 1.75;
}

.theme-toggle {
  font: inherit;
  font-size: 12px;
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  color: var(--ink-soft);
  background: var(--surface);
  border: 1px solid var(--rule);
  border-radius: 3px;
  padding: 5px 11px;
  cursor: pointer;
  margin-top: 6px;
}
.theme-toggle:hover { border-color: var(--brass); color: var(--brass); }
.theme-toggle:focus-visible { outline: 2px solid var(--brass); outline-offset: 2px; }

/* ── секции ───────────────────────────────────────────────────────────── */

section { display: flex; flex-direction: column; gap: 16px; }

.eyebrow {
  display: flex;
  align-items: baseline;
  gap: 12px;
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 11px;
  letter-spacing: .16em;
  text-transform: uppercase;
  color: var(--ink-faint);
}
.eyebrow::after {
  content: "";
  flex: 1;
  height: 1px;
  background: var(--rule);
}
.eyebrow .num { color: var(--brass); font-weight: 600; }

h2 {
  font-family: Archivo, "IBM Plex Sans", sans-serif;
  font-size: 19px;
  font-weight: 600;
  letter-spacing: -.01em;
  margin: 0;
}

/* ── риск-карточки ────────────────────────────────────────────────────── */

.risk-grid { display: flex; flex-direction: column; gap: 10px; }

.risk {
  background: var(--surface);
  border: 1px solid var(--rule-soft);
  border-left: 3px solid var(--rule);
  border-radius: 3px;
  padding: 13px 16px;
  box-shadow: var(--shadow);
}
.risk.high { border-left-color: var(--short); background: var(--surface); }
.risk.medium { border-left-color: var(--warn); }
.risk.info { border-left-color: var(--brass); }

.risk .tag {
  display: inline-block;
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 10.5px;
  letter-spacing: .1em;
  text-transform: uppercase;
  padding: 2px 7px;
  border-radius: 2px;
  margin-right: 9px;
  vertical-align: 1px;
}
.risk.high .tag { background: var(--short-soft); color: var(--short); }
.risk.medium .tag { background: var(--warn-soft); color: var(--warn); }
.risk.info .tag { background: var(--brass-soft); color: var(--brass); }
.risk strong { font-weight: 600; }
.risk .body { color: var(--ink-soft); }

/* ── таблица вотчлиста ────────────────────────────────────────────────── */

.table-scroll {
  overflow-x: auto;
  background: var(--surface);
  border: 1px solid var(--rule-soft);
  border-radius: 4px;
  box-shadow: var(--shadow);
}

table { border-collapse: collapse; width: 100%; min-width: 940px; }

thead th {
  position: sticky;
  top: 0;
  background: var(--surface-sunk);
  border-bottom: 1px solid var(--rule);
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 10.5px;
  font-weight: 500;
  letter-spacing: .1em;
  text-transform: uppercase;
  color: var(--ink-faint);
  text-align: left;
  padding: 11px 14px;
  white-space: nowrap;
}

tbody td {
  border-bottom: 1px solid var(--rule-soft);
  padding: 13px 14px;
  vertical-align: top;
}
tbody tr:last-child td { border-bottom: none; }
tbody tr.muted { opacity: .62; }

.ticker {
  font-family: Archivo, sans-serif;
  font-size: 15px;
  font-weight: 700;
  letter-spacing: -.01em;
  white-space: nowrap;
}
.ticker .side {
  display: block;
  font-family: "IBM Plex Mono", monospace;
  font-size: 10px;
  font-weight: 400;
  letter-spacing: .08em;
  text-transform: uppercase;
  color: var(--ink-faint);
  margin-top: 2px;
}

.num-cell {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-variant-numeric: tabular-nums;
  font-size: 13.5px;
  white-space: nowrap;
}
.up { color: var(--long); }
.down { color: var(--short); }

.trend { display: flex; flex-direction: column; gap: 3px; }
.badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-family: "IBM Plex Mono", monospace;
  font-size: 11px;
  letter-spacing: .04em;
  padding: 2px 7px;
  border-radius: 2px;
  white-space: nowrap;
  width: fit-content;
}
.badge .tf { opacity: .6; font-size: 9.5px; }
.badge.bull { background: var(--long-soft); color: var(--long); }
.badge.bear { background: var(--short-soft); color: var(--short); }
.badge.flat { background: var(--surface-sunk); color: var(--ink-faint); }

.score { display: flex; flex-direction: column; gap: 5px; min-width: 86px; }
.score .val {
  font-family: "IBM Plex Mono", monospace;
  font-variant-numeric: tabular-nums;
  font-size: 15px;
  font-weight: 600;
}
.score .val span { font-size: 11px; color: var(--ink-faint); font-weight: 400; }
.meter { height: 4px; background: var(--rule-soft); border-radius: 2px; overflow: hidden; }
.meter i { display: block; height: 100%; border-radius: 2px; }
.meter i.good { background: var(--long); }
.meter i.mid { background: var(--warn); }
.meter i.bad { background: var(--short); }

.setup { font-size: 13px; color: var(--ink-soft); min-width: 280px; }
.setup .levels {
  font-family: "IBM Plex Mono", monospace;
  font-variant-numeric: tabular-nums;
  font-size: 11.5px;
  color: var(--ink-faint);
  display: block;
  margin-top: 4px;
}
.setup .flag { display: block; margin-top: 5px; color: var(--warn); font-size: 12.5px; }
.setup .cat { display: block; margin-top: 5px; color: var(--brass); font-size: 12.5px; }

.spark { display: block; width: 84px; height: 30px; }

/* ── список избегаемых ────────────────────────────────────────────────── */

.avoid-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.avoid-list li {
  background: var(--surface);
  border: 1px solid var(--rule-soft);
  border-left: 3px solid var(--ink-faint);
  border-radius: 3px;
  padding: 11px 15px;
  font-size: 13.5px;
  color: var(--ink-soft);
}
.avoid-list li.binary { border-left-color: var(--short); }
.avoid-list b { color: var(--ink); font-family: Archivo, sans-serif; letter-spacing: -.01em; }

/* ── планы исполнения ─────────────────────────────────────────────────── */

.plans { display: flex; flex-direction: column; gap: 18px; }

.plan {
  background: var(--surface);
  border: 1px solid var(--rule-soft);
  border-radius: 4px;
  box-shadow: var(--shadow);
  overflow: hidden;
}

.plan-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px 14px;
  padding: 15px 18px;
  background: var(--surface-sunk);
  border-bottom: 1px solid var(--rule-soft);
}
.plan-head .sym {
  font-family: Archivo, sans-serif;
  font-size: 19px;
  font-weight: 700;
  letter-spacing: -.015em;
}
.plan-head .side {
  font-family: "IBM Plex Mono", monospace;
  font-size: 11px;
  letter-spacing: .09em;
  text-transform: uppercase;
  padding: 3px 8px;
  border-radius: 2px;
}
.plan-head .side.long { background: var(--long-soft); color: var(--long); }
.plan-head .side.short { background: var(--short-soft); color: var(--short); }
.plan-head .name { font-size: 14px; color: var(--ink-soft); }
.plan-head .rr {
  margin-left: auto;
  font-family: "IBM Plex Mono", monospace;
  font-variant-numeric: tabular-nums;
  font-size: 12.5px;
  color: var(--ink-faint);
}
.plan-head .rr b { color: var(--ink); font-weight: 600; }

/* лестница уровней */
.ladder { padding: 58px 22px 44px; border-bottom: 1px solid var(--rule-soft); }
.ladder-track {
  position: relative;
  height: 3px;
  background: linear-gradient(to right, var(--short-soft), var(--rule-soft), var(--long-soft));
  border-radius: 2px;
  margin: 0 8px;
}
.ladder-track.short-side {
  background: linear-gradient(to right, var(--long-soft), var(--rule-soft), var(--short-soft));
}
.mark { position: absolute; top: 50%; transform: translate(-50%, -50%); }
.mark .dot {
  width: 9px; height: 9px; border-radius: 50%;
  border: 2px solid var(--surface);
  display: block;
}
.mark.stop .dot { background: var(--short); }
.mark.trigger .dot { background: var(--brass); width: 11px; height: 11px; }
.mark.t1 .dot, .mark.t2 .dot { background: var(--long); }
.mark.last .dot { background: var(--ink-faint); width: 7px; height: 7px; }

.mark .lbl {
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
  bottom: 14px;
  font-family: "IBM Plex Mono", monospace;
  font-variant-numeric: tabular-nums;
  font-size: 10.5px;
  line-height: 1.3;
  white-space: nowrap;
  text-align: center;
  color: var(--ink-faint);
}
.mark .lbl b { display: block; font-size: 12px; color: var(--ink); font-weight: 600; }
.mark.below .lbl { bottom: auto; top: 14px; }
.mark.stop .lbl b { color: var(--short); }
.mark.trigger .lbl b { color: var(--brass); }
.mark.t1 .lbl b, .mark.t2 .lbl b { color: var(--long); }

.plan-rows { display: grid; grid-template-columns: 190px 1fr; }
.plan-rows dt {
  font-family: "IBM Plex Mono", monospace;
  font-size: 10.5px;
  letter-spacing: .08em;
  line-height: 1.5;
  text-transform: uppercase;
  color: var(--ink-faint);
  padding: 13px 18px 13px 18px;
  border-top: 1px solid var(--rule-soft);
}
.plan-rows dd {
  margin: 0;
  padding: 13px 18px 13px 0;
  border-top: 1px solid var(--rule-soft);
  font-size: 13.5px;
  color: var(--ink-soft);
}
.plan-rows dt:first-of-type, .plan-rows dd:first-of-type { border-top: none; }
.plan-rows dd b { color: var(--ink); font-weight: 600; }

.empty {
  background: var(--surface);
  border: 1px dashed var(--rule);
  border-radius: 4px;
  padding: 22px 18px;
  text-align: center;
  color: var(--ink-soft);
  font-size: 14px;
}

footer {
  border-top: 1px solid var(--rule);
  padding-top: 18px;
  font-size: 12px;
  color: var(--ink-faint);
  line-height: 1.7;
}

@media (max-width: 640px) {
  .wrap { padding: 0 14px 48px; gap: 32px; }
  .meta { text-align: left; }
  .plan-rows { grid-template-columns: 1fr; }
  .plan-rows dt { padding: 12px 18px 0; border-top: 1px solid var(--rule-soft); }
  .plan-rows dd { padding: 2px 18px 13px; border-top: none; }
  .plan-rows dd:first-of-type { border-top: none; }
}

@media (prefers-reduced-motion: reduce) {
  * { animation: none !important; transition: none !important; }
}
"""

TOGGLE_JS = """
(function () {
  var btn = document.getElementById('theme-toggle');
  if (!btn) return;
  btn.addEventListener('click', function () {
    var root = document.documentElement;
    var dark = root.getAttribute('data-theme') === 'dark';
    if (!root.getAttribute('data-theme')) {
      dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    }
    root.setAttribute('data-theme', dark ? 'light' : 'dark');
  });
})();
"""


# ════════════════════════════════════════════════════════════════════════════
#  ЭЛЕМЕНТЫ
# ════════════════════════════════════════════════════════════════════════════

def esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def sparkline(values: Sequence[float], rising: bool) -> str:
    """Спарклайн с заливкой и подсвеченной последней точкой."""
    pts = [v for v in values if v is not None]
    if len(pts) < 2:
        return '<span class="num-cell" style="color:var(--ink-faint)">—</span>'
    w, h, pad = 84.0, 30.0, 3.0
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    step = w / (len(pts) - 1)
    coords = [(i * step, pad + (h - 2 * pad) * (1 - (v - lo) / span))
              for i, v in enumerate(pts)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area = f"0,{h:.1f} " + line + f" {w:.1f},{h:.1f}"
    color = "var(--long)" if rising else "var(--short)"
    ex, ey = coords[-1]
    return (
        f'<svg class="spark" viewBox="0 0 {w:.0f} {h:.0f}" preserveAspectRatio="none" '
        f'role="img" aria-label="динамика за {len(pts)} сессий">'
        f'<polygon points="{area}" fill="{color}" opacity=".10"></polygon>'
        f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="1.4" '
        f'stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke">'
        f'</polyline>'
        f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="2.1" fill="{color}"></circle>'
        f'</svg>'
    )


def trend_badge(label: str, score: int, timeframe: str) -> str:
    cls = "bull" if score >= 1 else ("bear" if score <= -1 else "flat")
    return (f'<span class="badge {cls}"><span class="tf">{esc(timeframe)}</span>'
            f'{esc(label)}</span>')


def score_cell(score: float) -> str:
    cls = "good" if score >= 7 else ("mid" if score >= 5 else "bad")
    return (f'<div class="score"><div class="val">{score:.1f}<span>/10</span></div>'
            f'<div class="meter"><i class="{cls}" style="width:{score * 10:.0f}%"></i></div>'
            f'</div>')


def ladder(plan, fmt) -> str:
    """Лестница уровней: стоп → триггер → цели, с текущей ценой."""
    marks = [
        ("stop", "Стоп", plan.stop_level, ""),
        ("trigger", "Триггер", plan.trigger_level, ""),
        ("t1", "T1", plan.t1, plan.t1_name),
        ("t2", "T2", plan.t2, plan.t2_name),
    ]
    values = [v for _, _, v, _ in marks] + [plan.last]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0

    def pos(v: float) -> float:
        return (v - lo) / span * 100.0

    def align(p: float) -> str:
        """У краёв подпись прижимается к метке, иначе её обрежет контейнер."""
        if p <= 8.0:
            return "left:0;transform:translateX(0);text-align:left"
        if p >= 92.0:
            return "left:100%;transform:translateX(-100%);text-align:right"
        return ""

    # Текущая цена рисуется снизу, чтобы не наезжать на подписи уровней.
    p_last = pos(plan.last)
    parts = [f'<div class="mark last below" style="left:{p_last:.2f}%">'
             f'<span class="dot"></span>'
             f'<span class="lbl" style="{align(p_last)}">сейчас'
             f'<b>{esc(fmt(plan.last))}</b></span></div>']
    for cls, title, value, note in marks:
        note_txt = f'<br>{esc(note)}' if note else ""
        p = pos(value)
        parts.append(
            f'<div class="mark {cls}" style="left:{p:.2f}%">'
            f'<span class="dot"></span>'
            f'<span class="lbl" style="{align(p)}">{esc(title)}'
            f'<b>{esc(fmt(value))}</b>{note_txt}</span></div>')
    side = " short-side" if plan.direction == "short" else ""
    return (f'<div class="ladder"><div class="ladder-track{side}">'
            + "".join(parts) + '</div></div>')


# ════════════════════════════════════════════════════════════════════════════
#  СБОРКА СТРАНИЦЫ
# ════════════════════════════════════════════════════════════════════════════

def render_html(assessments: list["Assessment"], snaps: dict[str, "Snapshot"],
                events: "Events", missing: dict[str, str], as_of: date) -> str:
    # Импорт внутри функции: модуль остаётся необязательным для CLI-логики.
    from market_dashboard import (ROOT, build_plan, cfg, fmt_level, fmt_pct,
                                 fmt_quote, macro_lines, num)
    from charts import CHART_CSS, CHART_JS, color_tokens, performance_chart, price_chart
    from live_quotes import LIVE_CSS, LIVE_JS, load_live_config, render_live_panel
    from tradingview import TV_CSS, TV_JS, advanced_chart, ticker_tape, tv_symbol

    tradables = sorted([a for a in assessments if a.tradable],
                       key=lambda a: a.score, reverse=True)
    avoid = sorted([a for a in assessments if not a.tradable],
                   key=lambda a: a.score, reverse=True)
    ordered = sorted(assessments, key=lambda a: a.score, reverse=True)

    out: list[str] = []
    out.append(f'<title>Торговый бриф · {as_of.isoformat()}</title>')
    out.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    out.append('<link rel="preconnect" href="https://fonts.googleapis.com">')
    out.append('<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>')
    out.append('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
               'family=Archivo:wght@600;700&'
               'family=IBM+Plex+Mono:wght@400;500;600&'
               'family=IBM+Plex+Sans:wght@400;500;600&display=swap">')
    light_tokens, dark_tokens = color_tokens()
    out.append(f"<style>{CSS}{CHART_CSS}{LIVE_CSS}{TV_CSS}\n"
               f":root {{\n{light_tokens}\n}}\n"
               f"@media (prefers-color-scheme: dark) {{\n"
               f'  :root:not([data-theme="light"]) {{\n{dark_tokens}\n  }}\n}}\n'
               f':root[data-theme="dark"] {{\n{dark_tokens.replace("    ", "  ")}\n}}\n'
               "</style>")
    out.append('<div class="wrap">')

    # ── шапка ──────────────────────────────────────────────────────────────
    sources = sorted({a.snap.source for a in assessments}) or ["n/a"]
    out.append('<header class="masthead"><div>'
               f'<span class="date">{as_of.isoformat()}</span>'
               '<h1>Market Dashboard &amp; Trading Plan</h1>'
               '</div><div class="meta">'
               f'данные на закрытие {as_of.isoformat()}<br>'
               f'источник: {esc(", ".join(sources))}<br>'
               f'собрано {datetime.now().strftime("%Y-%m-%d %H:%M")}'
               '<br><button class="theme-toggle" id="theme-toggle" type="button">'
               'сменить тему</button>'
               '</div></header>')

    # ── 0. живые котировки ─────────────────────────────────────────────────
    live_cfg = load_live_config(ROOT / str(cfg("paths.live", "live.json")))
    tv_on = bool((live_cfg.get("tradingview") or {}).get("enabled", True))
    if tv_on:
        # TradingView покрывает и индексные CFD, и форекс — то, чего нет
        # у бесплатных источников собственной панели.
        tape: list[tuple[str, str]] = []
        for group in live_cfg.get("groups", []):
            for entry in group.get("symbols") or []:
                if isinstance(entry, dict) and entry.get("label"):
                    tape.append((str(entry["label"]), tv_symbol(str(entry["label"]), entry)))
        if tape:
            out.append(ticker_tape(tape, esc))
    else:
        live_html, _ = render_live_panel(live_cfg, esc)
        out.append(live_html)

    # ── 1. макро ───────────────────────────────────────────────────────────
    out.append('<section><div class="eyebrow"><span class="num">01</span>'
               'Macro &amp; Risk Warnings</div>'
               '<h2>Макро-фон и риск-окна</h2><div class="risk-grid">')
    for line in macro_lines(snaps, events, missing):
        level = "high" if "🔴" in line else ("medium" if "🟡" in line else "info")
        tag = {"high": "high impact", "medium": "medium", "info": "контекст"}[level]
        body = line.replace("🔴 HIGH · ", "").replace("🟡 MED · ", "").replace("⚪ LOW · ", "")
        body = _md_bold(esc(body))          # экранируем до разбора **жирного**
        out.append(f'<div class="risk {level}"><span class="tag">{tag}</span>'
                   f'<span class="body">{body}</span></div>')
    out.append('</div></section>')

    # ── 2. графики ─────────────────────────────────────────────────────────
    featured = next((a for a in tradables if build_plan(a)), ordered[0] if ordered else None)
    view = int(num(cfg("chart.view_bars"), 130))
    out.append('<section><div class="eyebrow"><span class="num">02</span>'
               'Charts</div><h2>Графики</h2>')
    if featured is not None and tv_on:
        out.append(advanced_chart(tv_symbol(featured.snap.symbol),
                                  featured.snap.symbol, esc))
    if featured is not None:
        fplan = build_plan(featured) if featured.tradable else None
        s = featured.snap
        sub = ("сетап дня — уровни плана нанесены на график"
               if fplan else "лучший по скору тикер вотчлиста")
        chg_cls = "up" if s.change_pct > 0 else ("down" if s.change_pct < 0 else "")
        out.append(
            '<div class="chart-card"><div class="chart-head">'
            f'<span class="t">{esc(s.symbol)}</span>'
            f'<span class="sub">дневной график · {esc(sub)}</span>'
            '<span class="right">'
            f'<span>{esc(fmt_quote(s.close, s.symbol, s.source_symbol))} '
            f'<span class="{chg_cls}">{esc(fmt_pct(s.change_pct))}</span></span>'
            '<button class="reset-zoom" type="button">сбросить зум</button>'
            '</span></div>'
            '<div class="readout"></div>'
            '<div class="legend">'
            '<span class="key"><i style="background:var(--ma-fast)"></i>20 EMA</span>'
            '<span class="key"><i style="background:var(--ma-mid)"></i>50 SMA</span>'
            '<span class="key"><i style="background:var(--ma-slow)"></i>200 SMA</span>'
            + ('<span class="key" style="color:var(--short)"><i class="dash"></i>стоп</span>'
               '<span class="key" style="color:var(--brass)"><i class="dash"></i>триггер</span>'
               '<span class="key" style="color:var(--long)"><i class="dash"></i>цели</span>'
               if fplan else "")
            + '</div>')
        out.append(price_chart(s, fplan, view))
        out.append('<div class="chart-hint">колесо или щипок — зум · тянуть — листать '
                   'историю · двойной клик — вернуться к исходному виду</div>')
        out.append('</div>')

    compare = [a.snap for a in ordered][:int(num(cfg("chart.compare_max"), 6))]
    if len(compare) >= 2:
        out.append(
            '<div class="chart-card"><div class="chart-head">'
            '<span class="t">Относительная динамика</span>'
            '<span class="sub">от левого края окна · база едет за зумом</span>'
            '<span class="right"><span>кто ведёт, кто отстаёт</span>'
            '<button class="reset-zoom" type="button">сбросить зум</button></span></div>'
            '<div class="readout"></div>')
        out.append(performance_chart(compare, int(num(cfg("chart.compare_max"), 6)), view))
        out.append('<div class="chart-hint">проценты пересчитываются от начала видимого '
                   'окна — отлистай назад, и сравнение перестроится</div>')
        out.append('</div>')
    out.append('</section>')

    # ── 3. вотчлист ────────────────────────────────────────────────────────
    out.append('<section><div class="eyebrow"><span class="num">03</span>'
               "Today's Watchlist &amp; Setup Quality</div>"
               '<h2>Вотчлист и качество сетапов</h2>'
               '<div class="table-scroll"><table><thead><tr>'
               '<th>Тикер</th><th>Цена</th><th>1D</th><th>1 мес</th><th>60 сессий</th>'
               '<th>Тренд 1D / 1W</th><th>Качество</th>'
               '<th>Катализаторы и техническая картина</th>'
               '</tr></thead><tbody>')
    for a in ordered:
        s = a.snap
        side = {"long": "лонг", "short": "шорт"}.get(a.direction, "нет направления")
        chg_cls = "up" if s.change_pct > 0 else ("down" if s.change_pct < 0 else "")
        mo = s.change_1m
        mo_txt = fmt_pct(mo) if mo is not None else "n/a"
        mo_cls = "" if mo is None else ("up" if mo > 0 else ("down" if mo < 0 else ""))
        levels = []
        if s.ema20 is not None:
            levels.append(f"20EMA {fmt_level(s.ema20)}")
        if s.sma50 is not None:
            levels.append(f"50SMA {fmt_level(s.sma50)}")
        if s.sma200 is not None:
            levels.append(f"200SMA {fmt_level(s.sma200)}")
        metrics = []
        if s.rsi14 is not None:
            metrics.append(f"RSI {s.rsi14:.0f}")
        if s.rvol is not None:
            metrics.append(f"RVOL {s.rvol:.2f}")
        if s.atr14:
            metrics.append(f"ATR {fmt_level(s.atr14)} ({s.atr_pct:.1f}%)")
        head = a.reasons[0] if a.reasons else "структура нейтральна"
        flag = (f'<span class="flag">⚠ {esc(a.warnings[0])}</span>' if a.warnings else "")
        cat = (f'<span class="cat">◆ {esc(a.catalysts[0])}</span>' if a.catalysts else "")
        row_cls = "" if a.tradable else ' class="muted"'
        out.append(
            f'<tr{row_cls}>'
            f'<td class="ticker">{esc(s.symbol)}<span class="side">{esc(side)}'
            + (f' · {esc(s.source_symbol)}' if s.source_symbol != s.symbol else '')
            + '</span></td>'
            f'<td class="num-cell">{esc(fmt_quote(s.close, s.symbol, s.source_symbol))}</td>'
            f'<td class="num-cell {chg_cls}">{esc(fmt_pct(s.change_pct))}</td>'
            f'<td class="num-cell {mo_cls}">{esc(mo_txt)}</td>'
            # Цвет — по самому окну, а не по сегодняшнему дню: иначе
            # у падающей бумаги спарклайн зеленеет от одного зелёного дня.
            f'<td>{sparkline(s.spark, bool(s.spark) and s.spark[-1] >= s.spark[0])}</td>'
            f'<td><div class="trend">'
            f'{trend_badge(s.trend_d, s.trend_d_score, "1D")}'
            f'{trend_badge(s.trend_w, s.trend_w_score, "1W")}</div></td>'
            f'<td>{score_cell(a.score)}</td>'
            f'<td class="setup">{esc(head)}'
            f'<span class="levels">{esc(" · ".join(levels + metrics))}</span>'
            f'{flag}{cat}</td></tr>')
    out.append('</tbody></table></div></section>')

    # ── 3. избегаем ────────────────────────────────────────────────────────
    out.append('<section><div class="eyebrow"><span class="num">04</span>'
               'Avoid Today — High Risk / No Edge</div>'
               '<h2>Сегодня не трогаем</h2>')
    if avoid:
        out.append('<ul class="avoid-list">')
        for a in avoid:
            reason = (a.binary_event and f"бинарное событие — {a.binary_event}") or \
                     (a.warnings[0] if a.warnings else
                      f"качество {a.score:.1f}/10 ниже порога "
                      f"{num(cfg('scoring.tradable_min'), 6.0):.1f} — нет асимметрии")
            extra = "" if a.binary_event else f" (score {a.score:.1f}/10)"
            cls = " class=\"binary\"" if a.binary_event else ""
            out.append(f'<li{cls}><b>{esc(a.snap.symbol)}</b> — {esc(reason)}{esc(extra)}.</li>')
        out.append('</ul>')
    else:
        out.append('<div class="empty">Тикеров с явным отсутствием edge не выявлено. '
                   'Это не значит «бери всё» — исполняем только то, что расписано ниже.</div>')
    out.append('</section>')

    # ── 4. план исполнения ─────────────────────────────────────────────────
    out.append('<section><div class="eyebrow"><span class="num">05</span>'
               'Actionable Execution Plan</div>'
               '<h2>План исполнения — «если это действительно случится»</h2>'
               '<div class="plans">')
    limit = int(num(cfg("scoring.plan_setups"), 3))
    min_rr = num(cfg("execution.min_reward_risk"), 1.5)
    planned = 0
    skipped = 0
    for a in tradables:
        if planned >= limit:
            break
        plan = build_plan(a)
        if not plan:
            continue
        if plan.reward_risk < min_rr:
            if skipped < 2:
                out.append(
                    f'<div class="empty"><b>{esc(a.snap.symbol)}</b> — сетап качественный '
                    f'({a.score:.1f}/10), но R:R до T1 всего {plan.reward_risk:.2f} '
                    f'при минимуме {min_rr:.1f}. Ждём вход ближе к 20 EMA — сегодня не берём.'
                    '</div>')
                skipped += 1
            continue
        side_cls = "long" if a.direction == "long" else "short"
        side_txt = "Bullish" if a.direction == "long" else "Bearish"
        out.append(
            f'<article class="plan"><div class="plan-head">'
            f'<span class="sym">{esc(a.snap.symbol)}</span>'
            f'<span class="side {side_cls}">{side_txt}</span>'
            f'<span class="name">{esc(plan.name)}</span>'
            f'<span class="rr">score <b>{a.score:.1f}</b>/10 · '
            f'R:R до T1 <b>{plan.reward_risk:.2f}</b></span></div>')
        out.append(ladder(plan, fmt_level))
        out.append(
            '<dl class="plan-rows">'
            f'<dt>Триггер</dt><dd>{_md_bold(esc(plan.trigger))}</dd>'
            f'<dt>Подтверждение</dt><dd>{esc(plan.confirmation)}</dd>'
            f'<dt>Стоп / инвалидация</dt><dd>{esc(plan.stop)}</dd>'
            f'<dt>Цели</dt><dd>{esc(plan.targets)}</dd>'
            '</dl></article>')
        planned += 1
    if planned == 0:
        out.append('<div class="empty">Ни один сетап не прошёл фильтр качества и R:R. '
                   'Сегодня сидим на руках — отсутствие сделки это тоже позиция.</div>')
    out.append('</div></section>')

    out.append('<footer>Уровни рассчитаны от реальной структуры цены: 20 EMA, '
               '20-дневные экстремумы, ATR. Все входы условные — нет триггера, нет сделки. '
               'Инструмент фильтрации информации, не финансовый совет.</footer>')
    out.append('</div>')
    out.append(f"<script>{TOGGLE_JS}{CHART_JS}{LIVE_JS}{TV_JS}</script>")
    return "\n".join(out)


def _md_bold(text: str) -> str:
    """**жирный** из markdown-строк макро-секции → <strong>."""
    parts = text.split("**")
    return "".join(p if i % 2 == 0 else f"<strong>{p}</strong>" for i, p in enumerate(parts))
