# -*- coding: utf-8 -*-
"""
Самодостаточная HTML-страница с состоянием бота.

Файл делается один раз командой `dashboard` и открывается двойным кликом:
внутри уже всё — счёт, стратегии, сделки, графики. Ни сервера, ни Python,
ни интернета для просмотра не нужно.

Всё рисуется на стороне Python (SVG + HTML), скриптов на странице нет,
поэтому она открывается в любом браузере и работает офлайн.
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path

from . import candlecache
from .config import Config
from .genome import Genome
from .pine import tv_site_url
from .storage import Storage


def esc(x) -> str:
    return html.escape(str(x), quote=True)


def money(v: float, digits: int = 2) -> str:
    return f"{v:,.{digits}f}".replace(",", " ")


def price_fmt(v: float) -> str:
    """Цена с разумным числом знаков: у BTC и у мем-коина они разные."""
    a = abs(v)
    digits = 2 if a >= 1000 else 3 if a >= 10 else 4 if a >= 1 else 6 if a >= 0.01 else 8
    return money(v, digits)


def when(ts_seconds: float) -> str:
    return datetime.fromtimestamp(ts_seconds, timezone.utc).strftime("%d.%m %H:%M")


# ════════════════════════════════════════════════════════════════════════════
#  Графики (чистый SVG, без библиотек)
# ════════════════════════════════════════════════════════════════════════════
def line_chart(points: list[tuple[float, float]], width: int = 900, height: int = 240,
               color: str = "#3fd0c9", label: str = "") -> str:
    if len(points) < 2:
        return '<div class="empty">данных пока нет</div>'
    pad = 34
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, x1 = min(xs), max(xs)
    lo, hi = min(ys), max(ys)
    span_x = (x1 - x0) or 1
    span_y = (hi - lo) or (abs(hi) or 1) * 0.02

    def px(x: float) -> float:
        return pad + (x - x0) / span_x * (width - pad * 2)

    def py(y: float) -> float:
        return height - pad - (y - lo) / span_y * (height - pad * 2)

    path = "".join(f"{'L' if i else 'M'}{px(x):.1f},{py(y):.1f}"
                   for i, (x, y) in enumerate(points))
    area = f"{path}L{px(x1):.1f},{height - pad}L{pad},{height - pad}Z"
    grid = "".join(
        f'<line class="grid" x1="{pad}" y1="{py(lo + span_y * f):.1f}" '
        f'x2="{width - pad}" y2="{py(lo + span_y * f):.1f}"/>' for f in (0.25, 0.5, 0.75))
    return f"""<svg viewBox="0 0 {width} {height}" class="chart" preserveAspectRatio="none"
  role="img" aria-label="{esc(label or 'график')}">
  {grid}
  <path class="area" d="{area}" fill="{color}22"/>
  <path d="{path}" fill="none" stroke="{color}" stroke-width="2"/>
  <text class="tick" x="{pad}" y="{pad - 12}">{money(hi, 4)}</text>
  <text class="tick" x="{pad}" y="{height - pad + 16}">{money(lo, 4)}</text>
  <text class="tick" x="{width - pad}" y="{height - pad + 16}" text-anchor="end">{when(x1 / 1000)}</text>
  <text class="tick" x="{pad + 90}" y="{height - pad + 16}">{when(x0 / 1000)}</text>
</svg>"""


def price_chart(candles: list[list[float]], trades: list[dict], width: int = 900,
                height: int = 260, position: dict | None = None) -> str:
    """Цена и метки сделок бота на ней: где купил и где продал."""
    if len(candles) < 3:
        return '<div class="empty">свечей в кэше нет — запусти `fetch`</div>'
    pad = 34
    xs = [c[0] for c in candles]
    x0, x1 = xs[0], xs[-1]
    # сделка случается сразу после закрытия свечи и может оказаться правее графика —
    # чуть растягиваем окно, но не больше чем на 15%, чтобы кривая не сплющилась
    if trades:
        newest = max(t["ts"] * 1000 for t in trades)
        if x1 < newest <= x1 + (x1 - x0) * 0.15:
            x1 = newest
    lows = [c[3] for c in candles]
    highs = [c[2] for c in candles]
    lo, hi = min(lows), max(highs)
    span_x = (x1 - x0) or 1
    span_y = (hi - lo) or 1

    def px(x: float) -> float:
        return pad + (x - x0) / span_x * (width - pad * 2)

    def py(y: float) -> float:
        return height - pad - (y - lo) / span_y * (height - pad * 2)

    path = "".join(f"{'L' if i else 'M'}{px(c[0]):.1f},{py(c[4]):.1f}"
                   for i, c in enumerate(candles))
    # связки «вход → выход»: видно путь каждой сделки
    conns = []
    stack: list[dict] = []
    for t in trades:
        if t["side"] == "buy":
            stack.append(t)
        elif stack:
            buy = stack.pop(0)
            if t["ts"] * 1000 >= x0:
                cls = "conn-win" if t["pnl"] >= 0 else "conn-loss"
                conns.append(f'<line class="{cls}" x1="{px(buy["ts"] * 1000):.1f}" '
                             f'y1="{py(buy["price"]):.1f}" x2="{px(t["ts"] * 1000):.1f}" '
                             f'y2="{py(t["price"]):.1f}"/>')

    levels = []
    used: list[float] = []
    if position:
        if position.get("opened_at"):
            xa = max(pad, px(position["opened_at"] * 1000))
            levels.append(f'<rect class="hold" x="{xa:.1f}" y="{pad}" '
                          f'width="{max(0.0, width - pad - xa):.1f}" height="{height - pad * 2}"/>')
        for name, key, cls in (("вход", "entry", "lvl-entry"), ("стоп", "stop", "lvl-stop"),
                               ("тейк", "take", "lvl-take")):
            v = float(position.get(key) or 0)
            if v <= 0 or not (lo <= v <= hi):
                continue
            ty = py(v) - 5
            while any(abs(u - ty) < 13 for u in used):     # подписи не должны наезжать
                ty -= 13
            used.append(ty)
            levels.append(f'<line class="{cls}" x1="{pad}" y1="{py(v):.1f}" '
                          f'x2="{width - pad}" y2="{py(v):.1f}"/>'
                          f'<text class="tick" x="{width - pad - 4}" y="{ty:.1f}" '
                          f'text-anchor="end">{name} {price_fmt(v)}</text>')

    marks = []
    shown = 0
    for t in trades:
        ts = t["ts"] * 1000
        if ts < x0 or ts > x1:
            continue
        x, y = px(ts), py(t["price"])
        shown += 1
        if t["side"] == "buy":
            marks.append(f'<path class="buy" d="M{x:.1f},{y + 12:.1f} l-5,9 l10,0 Z"/>'
                         f'<circle class="buy-dot" cx="{x:.1f}" cy="{y:.1f}" r="3"/>')
        else:
            color = "sell-win" if t["pnl"] >= 0 else "sell-loss"
            marks.append(f'<path class="{color}" d="M{x:.1f},{y - 12:.1f} l-5,-9 l10,0 Z"/>'
                         f'<circle class="{color}-dot" cx="{x:.1f}" cy="{y:.1f}" r="3"/>')
    hint = ""
    if trades and not shown:
        hint = ('<div class="empty">сделки есть, но они вне окна графика — '
                'обнови свечи командой <code>fetch</code></div>')
    return f"""<svg viewBox="0 0 {width} {height}" class="chart" preserveAspectRatio="none"
  role="img" aria-label="цена и сделки">
  {''.join(levels)}
  <path d="{path}" fill="none" stroke="#7aa2f7" stroke-width="1.6"/>
  {''.join(conns)}
  {''.join(marks)}
  <text class="tick" x="{pad}" y="{pad - 12}">{price_fmt(hi)}</text>
  <text class="tick" x="{pad}" y="{height - pad + 16}">{price_fmt(lo)}</text>
  <text class="tick" x="{width - pad}" y="{height - pad + 16}" text-anchor="end">{when(x1 / 1000)}</text>
</svg>{hint}"""


# ════════════════════════════════════════════════════════════════════════════
#  Сборка страницы
# ════════════════════════════════════════════════════════════════════════════
def collect(cfg: Config, store: Storage, mode: str = "cex") -> dict:
    """Собирает всё, что нужно странице, из базы бота и кэша свечей."""
    pairs = {}
    if mode == "dex":
        try:
            pairs = json.loads(Path(cfg.pairs_path).read_text(encoding="utf-8"))
        except (OSError, ValueError, AttributeError):
            pairs = {}

    def label(symbol: str) -> str:
        p = pairs.get(symbol)
        return f"{p.get('base_symbol', '?')}/{p.get('quote_symbol', '?')}" if p else symbol

    prefix = "dex" if mode == "dex" else getattr(cfg, "exchange", "cex")

    def candles_of(symbol: str, limit: int = 400) -> list[list[float]]:
        rows = candlecache.read(candlecache.path_for(cfg.cache_dir, prefix, symbol, cfg.timeframe))
        if len(rows) > limit:                       # прореживаем, чтобы файл не распухал
            step = len(rows) // limit + 1
            rows = rows[::step]
        return rows[-limit:]

    def last_price(symbol: str) -> float:
        p = pairs.get(symbol)
        if p and float(p.get("price_usd") or 0) > 0:
            return float(p["price_usd"])
        rows = candles_of(symbol, 5)
        return float(rows[-1][4]) if rows else 0.0

    trades = [{
        "ts": int(t["ts"]), "side": t["side"], "symbol": t["symbol"], "label": label(t["symbol"]),
        "qty": float(t["qty"] or 0), "price": float(t["price"] or 0), "pnl": float(t["pnl"] or 0),
        "reason": t["reason"] or "", "tx": t["order_id"] or "",
    } for t in store.recent_trades(200)]

    positions = []
    for row in store.all_positions():
        symbol = row["symbol"]
        price = last_price(symbol) or float(row["entry_price"])
        entry = float(row["entry_price"])
        positions.append({
            "symbol": symbol, "label": label(symbol), "qty": float(row["qty"]),
            "entry": entry, "price": price, "stop": float(row["stop"] or 0),
            "take": float(row["take"] or 0),
            "change": (price / entry - 1) * 100 if entry else 0.0,
            "url": (pairs.get(symbol) or {}).get("url", ""),
            "opened_at": int(row["opened_at"] or 0),
        })

    symbols = list(cfg.symbols) or sorted({t["symbol"] for t in trades})
    strategies = []
    for symbol in symbols:
        row = store.active_strategy(symbol)
        held = next((p for p in positions if p["symbol"] == symbol), None)
        links = []
        if mode == "dex":
            pair = pairs.get(symbol) or {}
            if pair.get("url"):
                links.append(("DexScreener", pair["url"]))
            chain, _, pool = symbol.partition(":")
            try:
                from .dex.geckoterminal import network_of              # noqa: PLC0415

                if pool:
                    links.append(("GeckoTerminal",
                                  f"https://www.geckoterminal.com/{network_of(chain)}/pools/{pool}"))
            except Exception:                                          # noqa: BLE001
                pass
            base = (pair.get("base_symbol") or "").upper()
            if base:
                links.append(("TradingView", tv_site_url(f"{base}USD", "", cfg.timeframe)))
        else:
            links.append(("TradingView",
                          tv_site_url(symbol, getattr(cfg, "exchange", ""), cfg.timeframe)))
        item = {"symbol": symbol, "label": label(symbol),
                "url": (pairs.get(symbol) or {}).get("url", ""),
                "candles": candles_of(symbol),
                "trades": [t for t in trades if t["symbol"] == symbol],
                "position": held, "links": links}
        if row:
            item.update({"id": row["id"], "score": float(row["score"] or 0),
                         "describe": Genome.from_json(row["genome"]).describe(),
                         "metrics": json.loads(row["metrics"] or "{}"),
                         "created": int(row["created_at"])})
        strategies.append(item)

    cash = float(store.get("paper_cash", cfg.start_balance) or cfg.start_balance)
    equity = cash + sum(p["qty"] * p["price"] for p in positions)
    start = float(store.get("paper_start", cfg.start_balance) or cfg.start_balance)
    return {
        "mode": mode, "cash": cash, "equity": equity, "start": start,
        "pnl_pct": (equity / start - 1) * 100 if start else 0.0,
        "positions": positions, "strategies": strategies, "trades": trades,
        "curve": [(int(r["ts"]) * 1000, float(r["equity"])) for r in store.equity_curve(500)],
        "paused": store.get("paused_reason"), "timeframe": cfg.timeframe,
        "quote": cfg.quote, "db": cfg.db_path,
        "title": "Citadel DEX" if mode == "dex" else "Citadel Trader",
        "engine": "dexbot.py" if mode == "dex" else "tradebot.py",
    }


CSS = """
:root{--bg:#0b0f14;--panel:#111820;--panel2:#0e141b;--line:#1e2a36;--text:#d7e2ee;
 --dim:#7d90a4;--accent:#3fd0c9;--blue:#7aa2f7;--green:#4ade80;--red:#f87171;--amber:#fbbf24;
 --mono:ui-monospace,"Cascadia Mono","Consolas","Liberation Mono",monospace;
 --sans:system-ui,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}
header{padding:16px 20px;border-bottom:1px solid var(--line);background:var(--panel2);
 display:flex;gap:14px;align-items:baseline;flex-wrap:wrap}
.brand{font-family:var(--mono);font-weight:700;letter-spacing:.14em;color:var(--accent)}
.meta{color:var(--dim);font-size:12px;font-family:var(--mono);margin-left:auto}
main{max-width:1180px;margin:0 auto;padding:20px;display:grid;gap:18px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:13px 15px}
.card .k{color:var(--dim);font-size:11px;letter-spacing:.07em;text-transform:uppercase}
.card .v{font-family:var(--mono);font-size:21px;margin-top:6px}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
section h2{margin:0;padding:12px 16px;font-size:12px;letter-spacing:.09em;text-transform:uppercase;
 color:var(--dim);border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:center}
section h2 .sp{margin-left:auto;text-transform:none;letter-spacing:0;font-weight:400}
.body{padding:14px 16px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--dim);font-weight:500;font-size:11px;padding:9px 16px;
 border-bottom:1px solid var(--line);text-transform:uppercase;letter-spacing:.05em}
td{padding:8px 16px;border-bottom:1px solid rgba(30,42,54,.5);font-family:var(--mono)}
tr:last-child td{border-bottom:0}
.num{text-align:right}.up{color:var(--green)}.down{color:var(--red)}.warn{color:var(--amber)}
.empty{padding:18px 16px;color:var(--dim)}
.chart{width:100%;height:auto;display:block}
.chart .grid{stroke:var(--line);stroke-dasharray:3 4}
.chart .tick{fill:var(--dim);font-size:11px;font-family:var(--mono)}
.chart .conn-win{stroke:var(--accent);stroke-width:1.3;stroke-dasharray:4 3;opacity:.75}
.chart .conn-loss{stroke:var(--red);stroke-width:1.3;stroke-dasharray:4 3;opacity:.75}
.chart .lvl-entry{stroke:#d7e2ee;stroke-width:1;stroke-dasharray:5 4;opacity:.75}
.chart .lvl-stop{stroke:var(--red);stroke-width:1;stroke-dasharray:5 4;opacity:.8}
.chart .lvl-take{stroke:var(--accent);stroke-width:1;stroke-dasharray:5 4;opacity:.8}
.chart .hold{fill:rgba(63,208,201,.07)}
.chart .buy{fill:var(--green)}.chart .buy-dot{fill:var(--green)}
.chart .sell-win{fill:var(--accent)}.chart .sell-win-dot{fill:var(--accent)}
.chart .sell-loss{fill:var(--red)}.chart .sell-loss-dot{fill:var(--red)}
.strat{padding:14px 16px;border-bottom:1px solid rgba(30,42,54,.5)}
.strat:last-child{border-bottom:0}
.strat .top{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.strat .top .sp{margin-left:auto;font-size:12px;color:var(--dim)}
.strat .name{font-family:var(--mono);font-size:15px}
.tag{display:inline-block;padding:2px 8px;border:1px solid var(--line);border-radius:999px;
 font-size:11px;color:var(--dim);font-family:var(--mono)}
.strat pre{margin:10px 0 0;font-family:var(--mono);font-size:12.5px;color:var(--dim);
 white-space:pre-wrap;line-height:1.6}
.legend{color:var(--dim);font-size:12px;padding:0 16px 12px}
.legend b{font-weight:600}
footer{color:var(--dim);font-size:12px;text-align:center;padding:6px 20px 26px;line-height:1.7}
code{font-family:var(--mono);background:var(--panel2);border:1px solid var(--line);
 border-radius:5px;padding:1px 5px}
@media print{body{background:#fff;color:#000}section{break-inside:avoid}}
"""


def render(data: dict, refresh_seconds: int = 0) -> str:
    d = data
    refresh = (f'<meta http-equiv="refresh" content="{refresh_seconds}">'
               if refresh_seconds else "")
    pnl_class = "up" if d["pnl_pct"] >= 0 else "down"
    paused = (f'<div class="card"><div class="k">Пауза</div>'
              f'<div class="v warn" style="font-size:14px">{esc(d["paused"])}</div></div>'
              if d.get("paused") else "")

    # ── позиции ─────────────────────────────────────────────────────────────
    if d["positions"]:
        rows = "".join(
            f'<tr><td>{link(p["label"], p["url"])}</td>'
            f'<td class="num">{money(p["qty"], 6)}</td>'
            f'<td class="num">{price_fmt(p["entry"])}</td>'
            f'<td class="num">{price_fmt(p["price"])}</td>'
            f'<td class="num">{price_fmt(p["stop"])}</td>'
            f'<td class="num {"up" if p["change"] >= 0 else "down"}">'
            f'{p["change"]:+.2f}%</td></tr>' for p in d["positions"])
        positions = ('<table><thead><tr><th>Пара</th><th class="num">Объём</th>'
                     '<th class="num">Вход</th><th class="num">Сейчас</th>'
                     '<th class="num">Стоп</th><th class="num">Изм.</th></tr></thead>'
                     f'<tbody>{rows}</tbody></table>')
    else:
        positions = '<div class="empty">открытых позиций нет</div>'

    # ── стратегии и графики ─────────────────────────────────────────────────
    blocks = []
    for s in d["strategies"]:
        if not s.get("id"):
            blocks.append(f'<div class="strat"><div class="top">'
                          f'<span class="name">{link(s["label"], s["url"])}</span>'
                          f'<span class="tag">стратегии нет — не торгую</span></div></div>')
            continue
        v = (s.get("metrics") or {}).get("valid") or {}
        t = (s.get("metrics") or {}).get("train") or {}
        blocks.append(f"""<div class="strat">
  <div class="top">
    <span class="name">{link(s["label"], s["url"])}</span>
    <span class="tag">стратегия #{s["id"]} · скор {s["score"]:.2f}</span>
    {metrics_tag("обучение", t)}{metrics_tag("валидация", v)}
    <span class="sp">{links_row(s.get("links"))}</span>
  </div>
  <pre>{esc(s["describe"])}</pre>
  {price_chart(s["candles"], s["trades"], position=s.get("position"))}
</div>""")
    strategies = "".join(blocks) or '<div class="empty">стратегий пока нет</div>'

    # ── сделки ──────────────────────────────────────────────────────────────
    if d["trades"]:
        rows = []
        for t in d["trades"][:60]:
            side = ('<span class="up">покупка</span>' if t["side"] == "buy"
                    else '<span class="down">продажа</span>')
            pnl = (f'<span class="{"up" if t["pnl"] >= 0 else "down"}">{t["pnl"]:+.2f}</span>'
                   if t["side"] == "sell" else "—")
            tx = (f' <a href="https://solscan.io/tx/{esc(t["tx"])}">tx</a>' if t["tx"] else "")
            rows.append(f'<tr><td>{when(t["ts"])}</td><td>{side}</td>'
                        f'<td>{esc(t["label"])}{tx}</td>'
                        f'<td class="num">{price_fmt(t["price"])}</td>'
                        f'<td class="num">{pnl}</td><td>{esc(t["reason"])}</td></tr>')
        trades = ('<table><thead><tr><th>Время (UTC)</th><th>Что</th><th>Пара</th>'
                  '<th class="num">Цена</th><th class="num">P&amp;L</th><th>Причина</th>'
                  f'</tr></thead><tbody>{"".join(rows)}</tbody></table>')
    else:
        trades = '<div class="empty">сделок пока нет</div>'

    generated = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{refresh}
<title>{esc(d["title"])} — состояние</title>
<style>{CSS}</style></head>
<body>
<header>
  <span class="brand">CITADEL</span>
  <span>{esc(d["title"])}</span>
  <span class="tag">{esc(d["timeframe"])}</span>
  <span class="meta">обновлено {generated}</span>
</header>
<main>
  <div class="cards">
    <div class="card"><div class="k">Эквити</div>
      <div class="v">{money(d["equity"])} {esc(d["quote"])}</div></div>
    <div class="card"><div class="k">Свободно</div><div class="v">{money(d["cash"])}</div></div>
    <div class="card"><div class="k">P&amp;L от старта</div>
      <div class="v {pnl_class}">{d["pnl_pct"]:+.2f}%</div></div>
    <div class="card"><div class="k">Позиций</div><div class="v">{len(d["positions"])}</div></div>
    <div class="card"><div class="k">Сделок всего</div><div class="v">{len(d["trades"])}</div></div>
    {paused}
  </div>

  <section><h2>Кривая счёта</h2>
    <div class="body">{line_chart(d["curve"], label="эквити")}</div></section>

  <section><h2>Позиции</h2>{positions}</section>

  <section><h2>Стратегии и сделки на графике</h2>
    <div class="legend">▲ зелёный — покупка · ▼ бирюзовый — продажа в плюс ·
      ▼ красный — продажа в минус · пунктир между ними — путь сделки.
      Подсветка справа и линии вход/стоп/тейк — открытая позиция.</div>
    {strategies}</section>

  <section><h2>Журнал сделок <span class="sp">последние {min(len(d["trades"]), 60)}</span></h2>
    {trades}</section>
</main>
<footer>
  Страница собрана ботом и работает без интернета: <code>python {esc(d["engine"])} dashboard</code>.<br>
  База: <code>{esc(d["db"])}</code>. Обнови файл этой же командой, чтобы увидеть свежие данные.<br>
  Не финансовый совет. Прибыльный бэктест ничего не гарантирует.
</footer>
</body></html>"""


def link(text: str, url: str = "") -> str:
    return f'<a href="{esc(url)}">{esc(text)}</a>' if url else esc(text)


def links_row(links) -> str:
    """Ссылки на настоящие графики этого инструмента."""
    if not links:
        return ""
    return "график: " + " · ".join(f'<a href="{esc(url)}">{esc(name)}</a>' for name, url in links)


def metrics_tag(title: str, m: dict) -> str:
    if not m:
        return ""
    return (f'<span class="tag">{esc(title)}: сделок {m.get("n_trades", 0)} · '
            f'доход {m.get("net_return", 0) * 100:+.1f}% · '
            f'просадка {m.get("max_dd", 0) * 100:.1f}% · '
            f'PF {m.get("profit_factor", 0):.2f}</span>')


def write(cfg: Config, store: Storage, path: str | Path | None = None, mode: str = "cex",
          refresh_seconds: int = 0) -> Path:
    """Собирает страницу и кладёт её в файл. Возвращает путь."""
    target = Path(path) if path else Path(cfg.db_path).parent / (
        "dashboard-dex.html" if mode == "dex" else "dashboard.html")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(collect(cfg, store, mode), refresh_seconds), encoding="utf-8")
    return target


def open_in_browser(path: Path) -> str:
    """Открывает файл в Microsoft Edge, иначе — в браузере по умолчанию."""
    import platform                                   # noqa: PLC0415
    import subprocess                                 # noqa: PLC0415
    import webbrowser                                 # noqa: PLC0415

    url = path.resolve().as_uri()
    system = platform.system()
    try:
        if system == "Windows":
            import os                                 # noqa: PLC0415
            os.startfile(f"microsoft-edge:{url}")     # noqa: S606
            return "Microsoft Edge"
        if system == "Darwin":
            subprocess.run(["open", "-a", "Microsoft Edge", str(path)], check=True,
                           capture_output=True)
            return "Microsoft Edge"
        for binary in ("microsoft-edge-stable", "microsoft-edge", "msedge"):
            try:
                subprocess.Popen([binary, url], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return "Microsoft Edge"
            except FileNotFoundError:
                continue
    except (OSError, subprocess.SubprocessError):
        pass
    return "браузер по умолчанию" if webbrowser.open(url) else ""


__all__ = ["collect", "render", "write", "open_in_browser", "line_chart", "price_chart"]
