#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Веб-панель статуса для memebot.py и socialbot.py.

Ничего сама не сканирует — просто читает heartbeat-файлы (data/*_status.json,
которые оба бота обновляют после каждого скана) и их SQLite-базы, и отдаёт
одну HTML-страницу с автообновлением. Боты и панель — независимые процессы,
запускать нужно и то, и другое.

Установка: зависимостей не добавляет (использует aiohttp, который уже нужен
memebot.py/socialbot.py).

Запуск:
    python dashboard.py                  # http://127.0.0.1:8080
    python dashboard.py --port 9000
    python dashboard.py --host 0.0.0.0   # доступ с других устройств в сети —
                                          # без пароля, используй только в доверенной сети
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

MEMEBOT_ONLINE_WINDOW = 5 * 60      # memebot по умолчанию сканирует раз в 60с
SOCIALBOT_ONLINE_WINDOW = 15 * 60   # socialbot — раз в 300с


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def memebot_alerts(limit: int = 12) -> list[dict]:
    db = DATA / "memebot.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT symbol, chain, score, price_usd, max_price, ts FROM alerts "
            "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    out = []
    for r in rows:
        p0, pmax = r["price_usd"] or 0, r["max_price"] or 0
        gain = ((pmax / p0 - 1) * 100) if p0 > 0 else 0
        out.append({"symbol": r["symbol"], "chain": r["chain"], "score": r["score"],
                    "gain": gain, "ts": r["ts"]})
    return out


def memebot_stats(hours: float = 24) -> dict:
    db = DATA / "memebot.db"
    if not db.exists():
        return {"total": 0}
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT price_usd, max_price FROM alerts WHERE ts >= ?",
            (time.time() - hours * 3600,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    gains = [((r["max_price"] or r["price_usd"]) / r["price_usd"] - 1) * 100
             for r in rows if (r["price_usd"] or 0) > 0]
    return {"total": len(rows), "wins_30": len([g for g in gains if g >= 30]),
            "avg_max": sum(gains) / len(gains) if gains else 0,
            "best": max(gains) if gains else 0}


def socialbot_recent(limit: int = 12) -> list[dict]:
    db = DATA / "socialbot.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT candidate_key, ts FROM seen ORDER BY ts DESC LIMIT ?",
            (limit,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    return [{"key": k, "ts": ts} for k, ts in rows]


def status_payload() -> dict:
    now = time.time()
    mb = read_json(DATA / "memebot_status.json")
    sb = read_json(DATA / "socialbot_status.json")
    return {
        "now": now,
        "memebot": {
            **mb,
            "online": bool(mb) and (now - mb.get("updated", 0)) < MEMEBOT_ONLINE_WINDOW,
            "alerts": memebot_alerts(),
            "stats24h": memebot_stats(24),
        },
        "socialbot": {
            **sb,
            "online": bool(sb) and (now - sb.get("updated", 0)) < SOCIALBOT_ONLINE_WINDOW,
            "recent": socialbot_recent(),
        },
    }


PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Citadel.aiii — статус ботов</title>
<style>
  :root {
    --bg: #0b0d12; --panel: #12151c; --panel-border: #232733;
    --text: #e6e8ee; --muted: #8b91a3; --accent: #5b8cff;
    --green: #3ecf8e; --red: #ff5c72; --yellow: #f5c451;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 32px 20px 60px; background: var(--bg); color: var(--text);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  .wrap { max-width: 980px; margin: 0 auto; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .sub { color: var(--muted); margin: 0 0 28px; font-size: 13px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  @media (max-width: 760px) { .grid { grid-template-columns: 1fr; } }
  .card {
    background: var(--panel); border: 1px solid var(--panel-border);
    border-radius: 12px; padding: 20px 22px; margin-bottom: 18px;
  }
  .card h2 { font-size: 16px; margin: 0 0 14px; display: flex; align-items: center; gap: 8px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; flex-shrink: 0; }
  .dot.on { background: var(--green); box-shadow: 0 0 8px var(--green); }
  .dot.off { background: var(--red); box-shadow: 0 0 8px var(--red); }
  .stats { display: flex; flex-wrap: wrap; gap: 22px; margin-bottom: 14px; }
  .stat { min-width: 80px; }
  .stat .v { font-size: 20px; font-weight: 600; }
  .stat .l { color: var(--muted); font-size: 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: var(--muted); font-weight: 500; font-size: 11px;
       text-transform: uppercase; letter-spacing: .03em; padding: 6px 8px; }
  td { padding: 7px 8px; border-top: 1px solid var(--panel-border); }
  tr:hover td { background: rgba(255,255,255,0.02); }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .pos { color: var(--green); } .neg { color: var(--red); }
  .empty { color: var(--muted); font-size: 13px; padding: 8px 0; }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px;
    background: rgba(91,140,255,0.15); color: var(--accent); margin-left: 6px;
  }
  footer { color: var(--muted); font-size: 12px; text-align: center; margin-top: 30px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Citadel.aiii</h1>
  <p class="sub">Статус ботов · обновляется каждые 5с · <span id="asof">—</span></p>

  <div class="grid">
    <div class="card">
      <h2><span id="mb-dot" class="dot off"></span> memebot.py <span class="badge">DexScreener</span></h2>
      <div class="stats" id="mb-stats"></div>
      <div id="mb-alerts"></div>
    </div>
    <div class="card">
      <h2><span id="sb-dot" class="dot off"></span> socialbot.py <span class="badge">TG/YouTube</span></h2>
      <div class="stats" id="sb-stats"></div>
      <div id="sb-recent"></div>
    </div>
  </div>

  <footer>Не финансовый совет · панель ничего не сканирует сама, только показывает статус запущенных ботов</footer>
</div>

<script>
function fmtAgo(ts) {
  if (!ts) return "—";
  const s = Math.max(0, Date.now()/1000 - ts);
  if (s < 60) return Math.round(s) + "с назад";
  if (s < 3600) return Math.round(s/60) + "м назад";
  if (s < 86400) return (s/3600).toFixed(1) + "ч назад";
  return (s/86400).toFixed(1) + "д назад";
}
function fmtPct(v) {
  const s = (v >= 0 ? "+" : "") + v.toFixed(1) + "%";
  return '<span class="' + (v >= 0 ? "pos" : "neg") + '">' + s + "</span>";
}
function esc(s) {
  const d = document.createElement("div"); d.innerText = s ?? ""; return d.innerHTML;
}

async function refresh() {
  let data;
  try {
    data = await (await fetch("/api/status")).json();
  } catch (e) { return; }

  document.getElementById("asof").innerText = new Date().toLocaleTimeString("ru-RU");

  const mb = data.memebot, sb = data.socialbot;

  document.getElementById("mb-dot").className = "dot " + (mb.online ? "on" : "off");
  document.getElementById("sb-dot").className = "dot " + (sb.online ? "on" : "off");

  const mbStats = document.getElementById("mb-stats");
  mbStats.innerHTML = mb.online ? [
    ["Аптайм", mb.started ? ((Date.now()/1000 - mb.started)/3600).toFixed(1) + "ч" : "—"],
    ["Сканов", mb.scans ?? 0],
    ["Алертов", mb.alerts_sent ?? 0],
    ["Порог", mb.threshold ?? "—"],
    ["Пар в скане", mb.last_seen_pairs ?? 0],
    ["24ч побед ≥30%", (mb.stats24h && mb.stats24h.wins_30) ?? 0],
  ].map(([l,v]) => `<div class="stat"><div class="v">${esc(v)}</div><div class="l">${l}</div></div>`).join("")
    : '<div class="empty">Бот не запущен или давно не сканировал (нет свежего heartbeat в data/memebot_status.json)</div>';

  const mbAlerts = document.getElementById("mb-alerts");
  if (mb.alerts && mb.alerts.length) {
    mbAlerts.innerHTML = "<table><tr><th>Монета</th><th>Сеть</th><th>Скор</th><th>Макс. рост</th><th>Когда</th></tr>" +
      mb.alerts.map(a => `<tr><td class="mono">$${esc(a.symbol)}</td><td>${esc(a.chain)}</td>` +
        `<td>${(a.score||0).toFixed(0)}</td><td>${fmtPct(a.gain||0)}</td><td>${fmtAgo(a.ts)}</td></tr>`).join("") +
      "</table>";
  } else {
    mbAlerts.innerHTML = '<div class="empty">Алертов ещё не было</div>';
  }

  const sbStats = document.getElementById("sb-stats");
  sbStats.innerHTML = sb.online ? [
    ["Аптайм", sb.started ? ((Date.now()/1000 - sb.started)/3600).toFixed(1) + "ч" : "—"],
    ["Сканов", sb.scans ?? 0],
    ["Алертов", sb.alerts_sent ?? 0],
    ["TG-режим", sb.tg_enabled ? (sb.tg_mode || "?") : "выкл"],
    ["YT-режим", sb.yt_enabled ? (sb.yt_mode || "?") : "выкл"],
    ["Упоминаний", sb.last_mentions ?? 0],
  ].map(([l,v]) => `<div class="stat"><div class="v">${esc(v)}</div><div class="l">${l}</div></div>`).join("")
    : '<div class="empty">Бот не запущен или давно не сканировал (нет свежего heartbeat в data/socialbot_status.json)</div>';

  const sbRecent = document.getElementById("sb-recent");
  if (sb.recent && sb.recent.length) {
    sbRecent.innerHTML = "<table><tr><th>Кандидат</th><th>Когда</th></tr>" +
      sb.recent.map(r => `<tr><td class="mono">${esc(r.key)}</td><td>${fmtAgo(r.ts)}</td></tr>`).join("") +
      "</table>";
  } else {
    sbRecent.innerHTML = '<div class="empty">Флагов ещё не было</div>';
  }
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


async def handle_index(_request: web.Request) -> web.Response:
    return web.Response(text=PAGE, content_type="text/html")


async def handle_api(_request: web.Request) -> web.Response:
    return web.json_response(status_payload())


STATIC_MARKER = (
    'async function refresh() {\n'
    '  let data;\n'
    '  try {\n'
    '    data = await (await fetch("/api/status")).json();\n'
    '  } catch (e) { return; }\n'
)


def render_static(payload: dict) -> str:
    """Автономный HTML-файл со снимком данных на момент генерации — без сервера,
    просто открыть двойным кликом. Не обновляется сам (для живой версии нужен
    `python dashboard.py` + запущенные боты)."""
    embedded = json.dumps(payload, ensure_ascii=False)
    page = PAGE.replace(
        STATIC_MARKER,
        f'async function refresh() {{\n  const data = {embedded};\n')
    page = page.replace(
        "setInterval(refresh, 5000);",
        "// статичный снимок — не обновляется автоматически")
    page = page.replace(
        '<p class="sub">Статус ботов · обновляется каждые 5с · <span id="asof">—</span></p>',
        '<p class="sub">Статичный снимок на момент генерации · '
        'для живого обновления запусти <code>python dashboard.py</code> рядом с ботами · '
        '<span id="asof">—</span></p>')
    return page


def main() -> None:
    ap = argparse.ArgumentParser(description="Веб-панель статуса memebot.py/socialbot.py")
    ap.add_argument("--host", default="127.0.0.1",
                    help="127.0.0.1 — только с этого компьютера; 0.0.0.0 — из локальной сети")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--static", metavar="FILE",
                    help="не запускать сервер — записать один самодостаточный HTML-снимок "
                        "текущего статуса в FILE и выйти")
    args = ap.parse_args()

    if args.static:
        Path(args.static).write_text(render_static(status_payload()), encoding="utf-8")
        print(f"Снимок записан: {args.static}")
        return

    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_api)
    print(f"Панель: http://{args.host}:{args.port}  (Ctrl+C — выход)")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
