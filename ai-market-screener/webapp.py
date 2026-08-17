#!/usr/bin/env python3
"""
Local web UI for the AI Market Screener.

Starts a small server on 127.0.0.1, opens the browser, and gives you a form
instead of command-line flags. The pipeline itself lives in main.py — this file
only adds the interface.

    python webapp.py

Binds to localhost only: nothing is exposed to the network.
"""

from __future__ import annotations

import html
import json
import os
import socket
import sys
import threading
import traceback
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import main as core

# When frozen by PyInstaller, __file__ points into a temp extraction directory.
# Config and reports must live next to the .exe instead.
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent

REPORTS = APP_DIR / "reports"
ENV_FILE = APP_DIR / ".env"
PORT = 8765

# ---------------------------------------------------------------------------
# page shell
# ---------------------------------------------------------------------------

CSS = """
:root{--bg:#F1F3F2;--surface:#FBFCFB;--surface-2:#E7ECEA;--rule:#D2DAD7;
--ink:#14211F;--ink-2:#4B5E5B;--ink-3:#7A8B88;--accent:#0B6B63;--accent-ink:#075049;
--accent-wash:#DCEAE7;--amber:#8A5715;--amber-wash:#F3E8D4;--red:#9A2E2E;--red-wash:#F4E0DE;
--fb:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
--fu:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
--fm:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
--bg:#0E1615;--surface:#141F1D;--surface-2:#1B2726;--rule:#2A3937;
--ink:#E2E9E7;--ink-2:#A0B3AF;--ink-3:#758884;--accent:#4FC0B2;--accent-ink:#7FD6CA;
--accent-wash:#12312D;--amber:#D8A650;--amber-wash:#33280F;--red:#E08A84;--red-wash:#38201E}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--fb);
font-size:16px;line-height:1.6;-webkit-font-smoothing:antialiased}
.wrap{max-width:900px;margin:0 auto;padding:40px 24px 80px}
header{border-bottom:1px solid var(--rule);padding-bottom:22px;margin-bottom:32px;
display:flex;flex-wrap:wrap;align-items:baseline;gap:14px;justify-content:space-between}
h1{font-size:1.9rem;margin:0;font-weight:600;letter-spacing:-.02em}
h1 a{color:inherit;text-decoration:none}
.tag{font-family:var(--fu);font-size:11px;font-weight:700;letter-spacing:.14em;
text-transform:uppercase;color:var(--accent)}
h2{font-size:1.35rem;margin:34px 0 10px;font-weight:600}
h3{font-family:var(--fu);font-size:.9rem;font-weight:700;margin:26px 0 10px}
p{margin:0 0 14px;max-width:66ch}
a{color:var(--accent)}
label{display:block;font-family:var(--fu);font-size:12px;font-weight:700;
letter-spacing:.06em;text-transform:uppercase;color:var(--ink-2);margin-bottom:6px}
.hint{font-family:var(--fu);font-size:12px;color:var(--ink-3);margin:5px 0 0}
textarea,input[type=text],select{width:100%;padding:10px 12px;background:var(--surface);
color:var(--ink);border:1px solid var(--rule);border-radius:2px;
font-family:var(--fm);font-size:13px;line-height:1.5}
textarea{min-height:132px;resize:vertical}
.row{display:grid;grid-template-columns:1fr;gap:18px;margin-bottom:18px}
@media(min-width:680px){.row.c3{grid-template-columns:1fr 1fr 1fr}
.row.c2{grid-template-columns:1fr 1fr}}
.check{display:flex;align-items:center;gap:9px;font-family:var(--fu);font-size:13.5px;
color:var(--ink-2);margin-bottom:14px;text-transform:none;letter-spacing:0;font-weight:400}
.check input{width:16px;height:16px;accent-color:var(--accent)}
button{font-family:var(--fu);font-size:14px;font-weight:700;letter-spacing:.03em;
padding:13px 26px;background:var(--accent);color:#fff;border:none;border-radius:2px;
cursor:pointer;transition:opacity .15s}
button:hover{opacity:.88}
button:disabled{opacity:.5;cursor:progress}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) button{color:#08201E}}
:root[data-theme="dark"] button{color:#08201E}
.card{background:var(--surface);border:1px solid var(--rule);padding:20px 22px;margin-bottom:22px}
.card.ok{border-left:3px solid var(--accent)}
.card.warn{border-left:3px solid var(--amber)}
.card.err{border-left:3px solid var(--red)}
.card h4{font-family:var(--fu);font-size:11px;font-weight:700;letter-spacing:.12em;
text-transform:uppercase;margin:0 0 8px}
.card.ok h4{color:var(--accent)}.card.warn h4{color:var(--amber)}.card.err h4{color:var(--red)}
pre{background:var(--surface-2);border:1px solid var(--rule);padding:14px 16px;
overflow-x:auto;font-family:var(--fm);font-size:12px;line-height:1.6;margin:0}
code{font-family:var(--fm);font-size:.9em}
table{border-collapse:collapse;width:100%;font-family:var(--fu);font-size:13.5px}
.tw{overflow-x:auto;border:1px solid var(--rule);background:var(--surface);margin-bottom:22px}
th{text-align:left;padding:10px 14px;background:var(--surface-2);font-size:11px;
font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:var(--ink-2);
border-bottom:1px solid var(--rule);white-space:nowrap}
td{padding:10px 14px;border-bottom:1px solid var(--rule);color:var(--ink-2);vertical-align:top}
tr:last-child td{border-bottom:none}
.score{font-family:var(--fm);font-weight:700;color:var(--ink)}
.pill{display:inline-block;padding:2px 9px;border-radius:2px;font-size:11px;font-weight:700}
.p-long{background:var(--accent-wash);color:var(--accent-ink)}
.p-short{background:var(--red-wash);color:var(--red)}
.p-none{background:var(--surface-2);color:var(--ink-3)}
.plan{border:1px solid var(--rule);background:var(--surface);padding:20px 22px;margin-bottom:16px}
.plan h4{font-family:var(--fu);font-size:15px;margin:0 0 12px;color:var(--ink)}
.plan dl{display:grid;grid-template-columns:1fr;gap:8px 18px;margin:0;font-size:14px}
@media(min-width:620px){.plan dl{grid-template-columns:auto 1fr}}
.plan dt{font-family:var(--fu);font-size:11px;font-weight:700;letter-spacing:.09em;
text-transform:uppercase;color:var(--ink-3);padding-top:3px}
.plan dd{margin:0;color:var(--ink-2)}
.meta{font-family:var(--fu);font-size:12px;color:var(--ink-3)}
footer{border-top:1px solid var(--rule);margin-top:44px;padding-top:20px;
font-family:var(--fu);font-size:12px;color:var(--ink-3)}
#busy{display:none;align-items:center;gap:12px;font-family:var(--fu);font-size:13px;
color:var(--ink-2);margin-top:18px}
.spin{width:15px;height:15px;border:2px solid var(--rule);border-top-color:var(--accent);
border-radius:50%;animation:sp .8s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.spin{animation:none}}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
"""


def page(body: str, title: str = "AI Market Screener") -> bytes:
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head><body>
<div class="wrap">
<header><h1><a href="/">AI Market Screener</a></h1>
<span class="tag">локально · 127.0.0.1:{PORT}</span></header>
{body}
<footer>Не является инвестиционной рекомендацией. Инструмент фильтрации информации:
все уровни и сценарии — гипотезы, а не сигналы.</footer>
</div></body></html>""".encode("utf-8")


def esc(value: object) -> str:
    return html.escape(str(value))


# ---------------------------------------------------------------------------
# form
# ---------------------------------------------------------------------------

def default_tickers() -> str:
    path = APP_DIR / "watchlist.txt"
    if path.exists():
        items = [
            ln.split("#")[0].strip()
            for ln in path.read_text(encoding="utf-8").splitlines()
        ]
        return "\n".join(i for i in items if i)
    return "AAPL\nMSFT\nNVDA\nAMD\nTSLA\nMETA\nAMZN\nGOOGL"


def key_present() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


def recent_reports(limit: int = 8) -> str:
    if not REPORTS.exists():
        return ""
    files = sorted(REPORTS.glob("plan_*.md"), reverse=True)[:limit]
    if not files:
        return ""
    rows = "\n".join(
        f'<tr><td><a href="/report?f={esc(f.name)}">{esc(f.stem.replace("plan_", ""))}</a></td>'
        f"<td>{f.stat().st_size // 1024} КБ</td></tr>"
        for f in files
    )
    return f"""<h2>Прошлые отчёты</h2><div class="tw"><table>
<thead><tr><th>Отчёт</th><th>Размер</th></tr></thead><tbody>{rows}</tbody></table></div>"""


def form_page(message: str = "") -> bytes:
    if key_present():
        banner = ""
    else:
        banner = """<div class="card warn"><h4>Нужен ключ API</h4>
<p>Ключ не найден. Возьмите его на
<a href="https://console.anthropic.com" target="_blank" rel="noopener">console.anthropic.com</a>
→ Settings → API Keys и вставьте ниже — он сохранится в файл <code>.env</code>
рядом с программой и больше спрашиваться не будет.</p>
<form method="post" action="/save-key">
<input type="text" name="key" placeholder="sk-ant-..." autocomplete="off" required>
<p class="hint">Ключ остаётся на этом компьютере и никуда не отправляется, кроме api.anthropic.com.</p>
<div style="margin-top:12px"><button type="submit">Сохранить ключ</button></div>
</form></div>"""

    return page(f"""{message}{banner}
<p>Выберите инструменты и нажмите «Собрать план». Прогон занимает от 30 секунд
до пары минут — данные загружаются, индикаторы считаются локально, затем снимок
уходит в Claude.</p>

<form method="post" action="/run" id="f">
<div class="row"><div>
  <label for="tickers">Инструменты</label>
  <textarea id="tickers" name="tickers" spellcheck="false">{esc(default_tickers())}</textarea>
  <p class="hint">По одному в строке. Акции и ETF — обычным символом (AAPL),
  крипта — парой биржи (BTC/USDT).</p>
</div></div>

<div class="row c3">
  <div><label for="effort">Глубина анализа</label>
    <select id="effort" name="effort">
      <option value="low">low — быстро и дёшево</option>
      <option value="medium">medium</option>
      <option value="high" selected>high — по умолчанию</option>
      <option value="xhigh">xhigh</option>
      <option value="max">max — дороже всего</option>
    </select></div>
  <div><label for="lang">Язык отчёта</label>
    <select id="lang" name="lang">
      <option value="ru" selected>Русский</option>
      <option value="en">English</option>
    </select></div>
  <div><label for="benchmarks">Бенчмарки</label>
    <input type="text" id="benchmarks" name="benchmarks" value="SPY,QQQ,^VIX">
    <p class="hint">Контекст, в watchlist не попадают.</p></div>
</div>

<label class="check"><input type="checkbox" name="no_news" value="1">
Не загружать заголовки новостей</label>
<label class="check"><input type="checkbox" name="dry" value="1">
Только проверить данные — без вызова API и без списания токенов</label>

<button type="submit" id="go">Собрать план</button>
<div id="busy"><span class="spin"></span><span>Работаю: загружаю котировки,
считаю индикаторы, жду ответ модели…</span></div>
</form>

{recent_reports()}

<script>
document.getElementById('f').addEventListener('submit', function () {{
  document.getElementById('go').disabled = true;
  document.getElementById('go').textContent = 'Считаю…';
  document.getElementById('busy').style.display = 'flex';
}});
</script>""")


# ---------------------------------------------------------------------------
# report rendering (from the plan JSON, no markdown parser needed)
# ---------------------------------------------------------------------------

REGIME_LABEL = {"risk_on": "🟢 RISK ON", "neutral": "🟡 NEUTRAL", "risk_off": "🔴 RISK OFF"}


def render_report(plan: dict, payload: dict, saved: str | None) -> str:
    macro = plan["macro"]
    out = [
        f'<p class="meta">Сессия {esc(payload["session_date"])} · '
        f'инструментов {len(payload["instruments"])} · '
        f'{esc(payload["as_of_utc"])} UTC</p>',
        '<div class="card ok"><h4>Макро-риск и правила дня</h4>',
        f'<p><strong>{REGIME_LABEL.get(macro["regime"], esc(macro["regime"]))}</strong></p>',
        f'<p>{esc(macro["summary"])}</p>',
    ]
    if macro["key_risks"]:
        out.append("<h3>Ключевые риски</h3><ul>")
        out += [f"<li>{esc(r)}</li>" for r in macro["key_risks"]]
        out.append("</ul>")
    if macro["rules_for_today"]:
        out.append("<h3>Правила на сегодня</h3><ol>")
        out += [f"<li>{esc(r)}</li>" for r in macro["rules_for_today"]]
        out.append("</ol>")
    out.append("</div>")

    out.append("<h2>Watchlist</h2><div class=\"tw\"><table>")
    out.append("<thead><tr><th>Тикер</th><th>Тренд</th><th>Score</th>"
               "<th>Bias</th><th>Сетап</th></tr></thead><tbody>")
    for row in sorted(plan["watchlist"], key=lambda r: -r["score"]):
        bias = row["bias"]
        out.append(
            f'<tr><td><code>{esc(row["ticker"])}</code></td><td>{esc(row["trend"])}</td>'
            f'<td class="score">{row["score"]}/10</td>'
            f'<td><span class="pill p-{esc(bias)}">{esc(bias)}</span></td>'
            f'<td>{esc(row["setup"])}<br><span class="meta">{esc(row["rationale"])}</span></td></tr>'
        )
    out.append("</tbody></table></div>")

    out.append("<h2>Не торгуем сегодня</h2>")
    if plan["avoid_today"]:
        out.append('<div class="card warn"><ul>')
        out += [f'<li><strong>{esc(i["ticker"])}</strong> — {esc(i["reason"])}</li>'
                for i in plan["avoid_today"]]
        out.append("</ul></div>")
    else:
        out.append('<p class="meta">Нет инструментов с явными противопоказаниями.</p>')

    out.append("<h2>Планы исполнения</h2>")
    if not plan["execution_plans"]:
        out.append('<div class="card warn"><h4>Пусто</h4>'
                   "<p>Валидных сетапов сегодня нет — торговать нечего. "
                   "Это нормальный и правильный результат.</p></div>")
    for item in plan["execution_plans"]:
        arrow = "🔼" if item["direction"] == "long" else "🔽"
        rows = "".join(
            f"<dt>{lbl}</dt><dd>{esc(item[key])}</dd>"
            for lbl, key in (
                ("Триггер", "trigger"), ("Подтверждение", "confirmation"),
                ("Стоп-лосс", "stop_loss"), ("Цель", "target"),
                ("Risk / Reward", "reward_risk"), ("Отмена идеи", "invalidation"),
            )
        )
        out.append(
            f'<div class="plan"><h4>{arrow} {esc(item["ticker"])} — '
            f'{esc(item["direction"].upper())}</h4><dl>{rows}</dl></div>'
        )

    if saved:
        out.append(f'<p class="meta">Сохранено: <code>{esc(saved)}</code></p>')
    out.append('<p style="margin-top:26px"><a href="/">← Новый прогон</a></p>')
    return "\n".join(out)


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------

def run_pipeline(fields: dict[str, list[str]]) -> bytes:
    def one(name: str, default: str = "") -> str:
        return (fields.get(name) or [default])[0].strip()

    tickers = [t.split("#")[0].strip().upper()
               for t in one("tickers").replace(",", "\n").splitlines()]
    tickers = list(dict.fromkeys(t for t in tickers if t))
    benchmarks = [b.strip().upper() for b in one("benchmarks").split(",") if b.strip()]
    effort = one("effort", "high")
    lang = one("lang", "ru")
    dry = bool(fields.get("dry"))
    no_news = bool(fields.get("no_news"))

    if not tickers:
        return form_page('<div class="card err"><h4>Пустой список</h4>'
                         "<p>Добавьте хотя бы один инструмент.</p></div>")
    if not dry and not key_present():
        return form_page('<div class="card err"><h4>Нет ключа API</h4>'
                         "<p>Сохраните ключ ниже или запустите проверку данных "
                         "галочкой «только проверить данные».</p></div>")

    bars = core.fetch_all(tickers + benchmarks, "binance")
    missing = [t for t in tickers if t not in bars]
    if not any(t in bars for t in tickers):
        return form_page(
            '<div class="card err"><h4>Данные не загрузились</h4>'
            f"<p>Ни один инструмент не удалось получить: {esc(', '.join(tickers))}. "
            "Проверьте интернет и правильность символов — у акций это обычный "
            "тикер (AAPL), у крипты пара с косой чертой (BTC/USDT).</p></div>")

    instruments = [core.build_snapshot(t, bars[t], core.ANALYSIS_WINDOW)
                   for t in tickers if t in bars]
    bench = [core.build_snapshot(b, bars[b], core.ANALYSIS_WINDOW)
             for b in benchmarks if b in bars]

    if not no_news:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for snap, news in zip(instruments,
                                  pool.map(lambda s: core.fetch_news(s["ticker"]), instruments)):
                snap["news"] = news

    payload = core.build_payload(instruments, bench, core.ANALYSIS_WINDOW)
    warn = ""
    if missing:
        warn = ('<div class="card warn"><h4>Пропущены</h4><p>Не удалось загрузить: '
                f"<code>{esc(', '.join(missing))}</code>. Остальные обработаны.</p></div>")

    if dry:
        blob = json.dumps(payload, ensure_ascii=False, indent=2)
        return page(f"""{warn}<h2>Проверка данных</h2>
<p class="meta">Это ровно тот снимок, который получила бы модель. API не вызывался,
токены не списаны.</p><pre>{esc(blob)}</pre>
<p style="margin-top:22px"><a href="/">← Назад</a></p>""", "Проверка данных")

    plan = core.analyse_with_claude(payload, lang, effort, core.MODEL)

    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    (REPORTS / f"plan_{stamp}.md").write_text(
        core.render_markdown(plan, payload, lang, core.MODEL), encoding="utf-8")
    (REPORTS / f"plan_{stamp}.json").write_text(
        json.dumps({"payload": payload, "plan": plan}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    return page(warn + render_report(plan, payload, f"reports/plan_{stamp}.md"),
                f"План — {payload['session_date']}")


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "MarketScreener"

    def _send(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        route = urlparse(self.path)
        if route.path == "/":
            self._send(form_page())
        elif route.path == "/report":
            name = (parse_qs(route.query).get("f") or [""])[0]
            target = (REPORTS / Path(name).name)          # basename only — no traversal
            if not target.is_file() or target.suffix != ".md":
                self._send(page('<div class="card err"><h4>Не найдено</h4>'
                                '<p><a href="/">← Назад</a></p></div>'), 404)
                return
            body = target.read_text(encoding="utf-8")
            self._send(page(f"<h2>{esc(target.stem)}</h2><pre>{esc(body)}</pre>"
                            '<p style="margin-top:22px"><a href="/">← Назад</a></p>',
                            target.stem))
        else:
            self._send(page('<div class="card err"><h4>Страница не найдена</h4>'
                            '<p><a href="/">← На главную</a></p></div>'), 404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        fields = parse_qs(self.rfile.read(length).decode("utf-8"))

        if urlparse(self.path).path == "/save-key":
            key = (fields.get("key") or [""])[0].strip()
            if not key.startswith("sk-ant-"):
                self._send(form_page('<div class="card err"><h4>Похоже, это не ключ</h4>'
                                     "<p>Ключ Anthropic начинается с <code>sk-ant-</code>.</p></div>"))
                return
            existing = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
            kept = [ln for ln in existing.splitlines()
                    if not ln.startswith("ANTHROPIC_API_KEY=")]
            ENV_FILE.write_text("\n".join(kept + [f"ANTHROPIC_API_KEY={key}"]) + "\n",
                                encoding="utf-8")
            os.environ["ANTHROPIC_API_KEY"] = key
            self._send(form_page('<div class="card ok"><h4>Ключ сохранён</h4>'
                                 f"<p>Записан в <code>{esc(ENV_FILE)}</code>. "
                                 "Можно собирать план.</p></div>"))
            return

        try:
            self._send(run_pipeline(fields))
        except Exception as exc:  # noqa: BLE001 - never show a bare traceback page
            traceback.print_exc()
            self._send(page(
                '<div class="card err"><h4>Прогон не удался</h4>'
                f"<p>{esc(exc)}</p>"
                "<p class=\"meta\">Подробности — в окне консоли, из которого "
                "запущена программа.</p>"
                '<p><a href="/">← Назад</a></p></div>', "Ошибка"), 500)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("  %s\n" % (fmt % args))


def free_port(start: int) -> int:
    for candidate in range(start, start + 20):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", candidate)) != 0:
                return candidate
    return start


def main() -> int:
    global PORT
    core.load_env(ENV_FILE)
    PORT = free_port(PORT)          # page() prints this in the header
    port = PORT
    url = f"http://127.0.0.1:{port}/"

    print("=" * 58)
    print("  AI Market Screener")
    print(f"  Открыто: {url}")
    print("  Чтобы остановить — закройте это окно или нажмите Ctrl+C")
    if not key_present():
        print("  Ключ API не найден — его можно вставить прямо на странице")
    print("=" * 58)

    # Headless build runners have no browser; opening one there just throws.
    if not os.getenv("SCREENER_NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
