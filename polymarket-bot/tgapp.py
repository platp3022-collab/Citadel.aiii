#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Polybot в Telegram: тот же движок из polybot.py, но управление и панель — в мессенджере.

Даёт две вещи сразу:
    1. Обычного бота: команды /status, /positions, /trades, /stats, /pause, /flat
       плюс автоматические уведомления о каждом входе и выходе.
    2. Mini App («тг апп») — панель, которая открывается прямо внутри Telegram кнопкой
       «Открыть панель»: позиции, кривая эквити, метрики, кнопки паузы и закрытия.

Настройка (.env рядом со скриптом):
    TELEGRAM_BOT_TOKEN=123456:AA...     # токен от @BotFather
    TELEGRAM_CHAT_ID=123456789          # ТВОЙ chat_id — только он управляет ботом
    WEBAPP_PUBLIC_URL=                  # https-адрес панели; без него работают команды
    WEBAPP_HOST=0.0.0.0
    WEBAPP_PORT=8080

Как получить https-адрес для Mini App (Telegram не открывает http):
    - на своём сервере: домен + сертификат (nginx/caddy перед портом 8080), или
    - на домашнем компьютере: cloudflared tunnel --url http://localhost:8080
      и вставить выданный https-адрес в WEBAPP_PUBLIC_URL.

Запуск:
    python tgapp.py             # бумажная торговля + бот + панель
    python tgapp.py --no-web    # только бот, без Mini App
    python tgapp.py --live      # боевые ордера (предохранитель тот же, см. README)

Управление ботом доступно только TELEGRAM_CHAT_ID: он двигает реальные деньги в --live.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any

import polybot as pb

try:
    from aiohttp import web
except ImportError:
    sys.exit("Нужен aiohttp:  pip install -r requirements.txt")

BASE_DIR = Path(__file__).resolve().parent
WEBAPP_DIR = BASE_DIR / "webapp"
TG_API = "https://api.telegram.org"

log = logging.getLogger("polybot.tg")


# --------------------------------------------------------------------------------------
# Клиент Telegram Bot API
# --------------------------------------------------------------------------------------
class Telegram:
    def __init__(self, token: str, http: pb.Http) -> None:
        self.token = token
        self.http = http
        self.offset = 0

    async def api(self, method: str, **params: Any) -> Any:
        url = f"{TG_API}/bot{self.token}/{method}"
        clean = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                 for k, v in params.items() if v is not None}
        result = await self.http.get_json(url, clean)
        if isinstance(result, dict):
            if result.get("ok"):
                return result.get("result")
            log.warning("telegram %s: %s", method, result.get("description"))
        return None

    async def send(self, chat_id: str, text: str, markup: dict | None = None) -> dict | None:
        return await self.api("sendMessage", chat_id=chat_id, text=text,
                              parse_mode="HTML", disable_web_page_preview=True,
                              reply_markup=markup)

    async def edit(self, chat_id: str, message_id: int, text: str,
                   markup: dict | None = None) -> Any:
        return await self.api("editMessageText", chat_id=chat_id, message_id=message_id,
                              text=text, parse_mode="HTML",
                              disable_web_page_preview=True, reply_markup=markup)

    async def answer_callback(self, callback_id: str, text: str = "") -> Any:
        return await self.api("answerCallbackQuery", callback_query_id=callback_id, text=text)

    async def set_commands(self) -> None:
        await self.api("setMyCommands", commands=[
            {"command": "panel", "description": "живая панель"},
            {"command": "status", "description": "эквити, позиции, режим"},
            {"command": "positions", "description": "открытые позиции"},
            {"command": "trades", "description": "последние сделки"},
            {"command": "stats", "description": "метрики и стратегии"},
            {"command": "pause", "description": "пауза: не открывать новое"},
            {"command": "resume", "description": "снять паузу"},
            {"command": "flat", "description": "закрыть все позиции"},
            {"command": "help", "description": "как это работает"},
        ])

    async def poll(self, timeout: int = 15) -> list[dict[str, Any]]:
        # long polling: держим запрос открытым, но короче общего таймаута Http (20с)
        url = f"{TG_API}/bot{self.token}/getUpdates"
        result = await self.http.get_json(
            url, {"offset": self.offset, "timeout": timeout, "limit": 20}, attempts=1
        )
        updates = result.get("result") if isinstance(result, dict) and result.get("ok") else None
        if not updates:
            return []
        self.offset = updates[-1]["update_id"] + 1
        return updates


# --------------------------------------------------------------------------------------
# Состояние движка → JSON для Mini App и текст для сообщений
# --------------------------------------------------------------------------------------
def state_dict(engine: pb.Engine) -> dict[str, Any]:
    pf = engine.portfolio
    cfg = engine.cfg
    total = pf.equity - cfg.bankroll
    return {
        "mode": "LIVE" if engine.broker.live else "PAPER",
        "paused": engine.paused,
        "blocked": engine.risk.blocked_reason,
        "status": engine.status_line(),
        "cycles": engine.cycles,
        "markets": len(engine.universe),
        "bankroll": round(cfg.bankroll, 2),
        "equity": round(pf.equity, 2),
        "cash": round(pf.cash, 2),
        "exposure": round(pf.exposure, 2),
        "pnl": round(total, 2),
        "pnl_pct": round(total / max(cfg.bankroll, 1) * 100, 2),
        "unrealized": round(pf.unrealized, 2),
        "realized": round(pf.realized, 2),
        "win_rate": round(pf.win_rate * 100, 1),
        "profit_factor": round(pf.profit_factor, 2),
        "max_dd": round(pf.max_drawdown * 100, 2),
        "sharpe": round(pf.sharpe, 2),
        "equity_curve": [round(v, 2) for v in pf.equity_curve[-240:]],
        "positions": [
            {
                "token_id": p.token_id,
                "question": p.question,
                "outcome": p.outcome,
                "strategy": p.strategy,
                "entry": round(p.entry, 3),
                "mark": round(p.mark or p.entry, 3),
                "size": round(p.size, 1),
                "cost": round(p.cost, 2),
                "upnl": round(p.upnl(), 2),
                "age_min": round(p.age_min, 1),
            }
            for p in sorted(pf.positions.values(), key=lambda x: -abs(x.upnl()))
        ],
        "fills": [
            {
                "ts": f.ts,
                "question": f.question,
                "side": f.side,
                "size": round(f.size, 1),
                "price": round(f.price, 3),
                "pnl": round(f.pnl, 2),
                "strategy": f.strategy,
            }
            for f in pf.fills[-25:][::-1]
        ],
        "strategies": [
            {
                "name": s.name,
                "title": s.title,
                "signals": s.signals,
                "trades": int(pf.stats_for(s.name)["trades"]),
                "pnl": round(pf.stats_for(s.name)["pnl"], 2),
                "notes": s.notes[-4:][::-1],
            }
            for s in engine.strategies
        ],
        "limits": {
            "risk_per_trade": cfg.risk_per_trade * 100,
            "max_positions": cfg.max_positions,
            "daily_loss_limit": cfg.daily_loss_limit * 100,
            "max_drawdown": cfg.max_drawdown * 100,
            "take_profit": cfg.take_profit,
            "stop_loss": cfg.stop_loss,
        },
    }


def esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


def render_status(engine: pb.Engine) -> str:
    s = state_dict(engine)
    sign = "🟢" if s["pnl"] >= 0 else "🔴"
    mode = "🔥 БОЕВОЙ" if s["mode"] == "LIVE" else "📄 БУМАЖНЫЙ"
    lines = [
        f"<b>Polybot</b> · {mode}",
        f"{sign} эквити <b>${s['equity']:,.2f}</b>  "
        f"({pb.money(s['pnl'])}, {s['pnl_pct']:+.2f}%)",
        f"кэш ${s['cash']:,.2f} · в рынке ${s['exposure']:,.2f} · позиций {len(s['positions'])}",
        f"реализовано {pb.money(s['realized'])} · плавающий {pb.money(s['unrealized'])}",
        f"рынков в работе: {s['markets']} · циклов: {s['cycles']}",
        "",
        f"<i>{esc(s['status'])}</i>",
    ]
    return "\n".join(lines)


def render_positions(engine: pb.Engine) -> str:
    s = state_dict(engine)
    if not s["positions"]:
        return "Открытых позиций нет — движок ждёт сигнал."
    rows = ["<b>Открытые позиции</b>", ""]
    for p in s["positions"]:
        mark = "🟢" if p["upnl"] >= 0 else "🔴"
        rows.append(
            f"{mark} <b>{esc(pb.short(p['question'], 60))}</b>\n"
            f"    <code>{esc(p['outcome'])}</code> · {esc(p['strategy'])} · "
            f"вход {p['entry']:.3f} → {p['mark']:.3f}\n"
            f"    ${p['cost']:,.0f} · {pb.money(p['upnl'])} · {p['age_min']:.0f} мин"
        )
    return "\n".join(rows)


def render_trades(engine: pb.Engine) -> str:
    s = state_dict(engine)
    if not s["fills"]:
        return "Сделок пока не было."
    rows = ["<b>Последние сделки</b>", "<pre>"]
    for f in s["fills"][:12]:
        when = pb.datetime.fromtimestamp(f["ts"]).strftime("%d.%m %H:%M")
        pnl = f"{pb.money(f['pnl']):>9}" if f["pnl"] else " " * 9
        rows.append(f"{when} {f['side']:<4} {f['size']:>6.0f} × {f['price']:.3f} {pnl}  "
                    f"{esc(pb.short(f['question'], 28))}")
    rows.append("</pre>")
    return "\n".join(rows)


def render_stats(engine: pb.Engine) -> str:
    s = state_dict(engine)
    lines = [
        "<b>Метрики</b>",
        f"win rate <b>{s['win_rate']:.0f}%</b> · profit factor <b>{s['profit_factor']:.2f}</b>",
        f"макс. просадка <b>{s['max_dd']:.1f}%</b> · sharpe <b>{s['sharpe']:.2f}</b>",
        "",
        "<b>Стратегии</b>",
    ]
    for st in s["strategies"]:
        lines.append(f"· <b>{esc(st['title'])}</b> — сигналов {st['signals']}, "
                     f"сделок {st['trades']}, {pb.money(st['pnl'])}")
        for note in st["notes"][:2]:
            lines.append(f"    <i>{esc(pb.short(note, 60))}</i>")
    lim = s["limits"]
    lines += [
        "",
        "<b>Лимиты</b>",
        f"риск на сделку {lim['risk_per_trade']:.1f}% · позиций максимум {lim['max_positions']}",
        f"дневной стоп {lim['daily_loss_limit']:.0f}% · стоп по просадке {lim['max_drawdown']:.0f}%",
        f"тейк +{lim['take_profit']*100:.0f}c · стоп −{lim['stop_loss']*100:.0f}c",
    ]
    return "\n".join(lines)


def render_panel(engine: pb.Engine) -> str:
    """Компактная живая панель — это сообщение бот редактирует на месте."""
    s = state_dict(engine)
    sign = "🟢" if s["pnl"] >= 0 else "🔴"
    head = (f"<b>Polybot</b> {'🔥' if s['mode'] == 'LIVE' else '📄'} "
            f"{sign} <b>${s['equity']:,.2f}</b> ({s['pnl_pct']:+.2f}%)")
    curve = pb.sparkline(s["equity_curve"], 28) if len(s["equity_curve"]) > 2 else ""
    body = [head, f"<code>{curve}</code>" if curve else "",
            f"кэш ${s['cash']:,.0f} · в рынке ${s['exposure']:,.0f} · "
            f"win {s['win_rate']:.0f}% · PF {s['profit_factor']:.2f}", ""]
    if s["positions"]:
        body.append("<b>Позиции</b>")
        for p in s["positions"][:5]:
            mark = "🟢" if p["upnl"] >= 0 else "🔴"
            body.append(f"{mark} {esc(pb.short(p['question'], 34))} "
                        f"<code>{esc(p['outcome'])}</code> "
                        f"{p['entry']:.2f}→{p['mark']:.2f} {pb.money(p['upnl'])}")
    else:
        body.append("<i>позиций нет — ждём сигнал</i>")
    body += ["", f"<i>{esc(pb.short(s['status'], 80))} · "
                 f"{pb.datetime.now().strftime('%H:%M:%S')}</i>"]
    return "\n".join(line for line in body if line != "" or True)


def panel_markup(engine: pb.Engine, public_url: str) -> dict[str, Any]:
    pause_btn = ({"text": "▶️ Продолжить", "callback_data": "resume"} if engine.paused
                 else {"text": "⏸ Пауза", "callback_data": "pause"})
    rows = [
        [{"text": "🔄 Обновить", "callback_data": "refresh"}, pause_btn],
        [{"text": "📉 Закрыть всё", "callback_data": "flat"},
         {"text": "📊 Метрики", "callback_data": "stats"}],
    ]
    if public_url:
        rows.insert(0, [{"text": "🖥 Открыть панель", "web_app": {"url": public_url}}])
    return {"inline_keyboard": rows}


# --------------------------------------------------------------------------------------
# Проверка подписи Mini App
# --------------------------------------------------------------------------------------
def check_init_data(init_data: str, token: str, max_age: float = 86400) -> dict[str, Any] | None:
    """Валидация initData из Telegram WebApp (HMAC по схеме Bot API). None — не доверяем.

    Без этой проверки панель мог бы дёрнуть кто угодно, зная адрес: она управляет деньгами.
    """
    if not init_data or not token:
        return None
    try:
        pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    data = dict(pairs)
    received = data.pop("hash", "")
    if not received:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received):
        return None
    try:
        if pb.now() - float(data.get("auth_date", 0)) > max_age:
            return None
    except (TypeError, ValueError):
        return None
    try:
        data["user"] = json.loads(data.get("user", "{}"))
    except ValueError:
        data["user"] = {}
    return data


# --------------------------------------------------------------------------------------
# Бот
# --------------------------------------------------------------------------------------
HELP = """<b>Polybot — торговый бот Polymarket в Telegram</b>

Движок крутится на твоём компьютере или сервере, Telegram — только пульт и витрина.

/panel — живая панель, обновляется сама
/status — эквити, кэш, позиции
/positions — что открыто прямо сейчас
/trades — последние сделки
/stats — метрики и работа стратегий
/pause — не открывать новое (открытое продолжаем вести)
/resume — снять паузу
/flat — закрыть все позиции по рынку
/help — эта справка

Кнопка «🖥 Открыть панель» открывает Mini App внутри Telegram — там позиции, кривая
эквити и те же кнопки управления.

В бумажном режиме сделки виртуальные, деньги не тратятся. Боевой включается отдельно —
ключом в .env и предохранителем POLYBOT_CONFIRM_LIVE=yes."""


class TgBot:
    def __init__(self, engine: pb.Engine, tg: Telegram, owner: str, public_url: str) -> None:
        self.engine = engine
        self.tg = tg
        self.owner = str(owner)
        self.public_url = public_url
        self.panel_msg: int | None = None
        self.seen_fills = len(engine.portfolio.fills)
        self.last_blocked = ""

    def is_owner(self, chat_id: Any) -> bool:
        return str(chat_id) == self.owner

    # --- команды ----------------------------------------------------------------------
    async def handle_command(self, chat_id: str, text: str) -> None:
        cmd = text.split()[0].lower().split("@")[0]
        eng = self.engine
        if cmd in ("/start", "/help"):
            await self.tg.send(chat_id, HELP, panel_markup(eng, self.public_url))
        elif cmd == "/panel":
            msg = await self.tg.send(chat_id, render_panel(eng),
                                     panel_markup(eng, self.public_url))
            if msg:
                self.panel_msg = msg.get("message_id")
        elif cmd == "/status":
            await self.tg.send(chat_id, render_status(eng))
        elif cmd == "/positions":
            await self.tg.send(chat_id, render_positions(eng))
        elif cmd == "/trades":
            await self.tg.send(chat_id, render_trades(eng))
        elif cmd == "/stats":
            await self.tg.send(chat_id, render_stats(eng))
        elif cmd == "/pause":
            eng.paused = True
            await self.tg.send(chat_id, "⏸ Пауза. Новые входы отключены, "
                                        "открытые позиции продолжаю вести.")
        elif cmd == "/resume":
            eng.paused = False
            await self.tg.send(chat_id, "▶️ Работаю дальше.")
        elif cmd == "/flat":
            closed = await eng.flatten("команда /flat")
            await self.tg.send(chat_id, f"📉 Закрыто позиций: {closed}.\n\n"
                                        + render_status(eng))
        else:
            await self.tg.send(chat_id, "Не знаю такой команды. /help")

    async def handle_callback(self, query: dict[str, Any]) -> None:
        chat_id = str(query.get("message", {}).get("chat", {}).get("id", ""))
        if not self.is_owner(chat_id):
            await self.tg.answer_callback(query["id"], "Эта панель не твоя.")
            return
        action = query.get("data", "")
        eng = self.engine
        note = "обновлено"
        if action == "pause":
            eng.paused = True
            note = "пауза"
        elif action == "resume":
            eng.paused = False
            note = "продолжаю"
        elif action == "flat":
            closed = await eng.flatten("кнопка «Закрыть всё»")
            note = f"закрыто: {closed}"
        elif action == "stats":
            await self.tg.send(chat_id, render_stats(eng))
        await self.tg.answer_callback(query["id"], note)
        message_id = query.get("message", {}).get("message_id")
        if message_id and action != "stats":
            await self.tg.edit(chat_id, message_id, render_panel(eng),
                               panel_markup(eng, self.public_url))

    # --- фоновые задачи ---------------------------------------------------------------
    async def poll_loop(self) -> None:
        await self.tg.set_commands()
        while not self.engine.stopping:
            updates = await self.tg.poll()
            for update in updates:
                try:
                    if "callback_query" in update:
                        await self.handle_callback(update["callback_query"])
                        continue
                    message = update.get("message") or update.get("edited_message")
                    if not message:
                        continue
                    chat_id = str(message.get("chat", {}).get("id", ""))
                    text = (message.get("text") or "").strip()
                    if not text:
                        continue
                    if not self.is_owner(chat_id):
                        await self.tg.send(chat_id, "Этот бот приватный: он управляет чужими "
                                                    "деньгами. Твой chat_id: "
                                                    f"<code>{esc(chat_id)}</code>")
                        continue
                    await self.handle_command(chat_id, text)
                except Exception as exc:
                    log.exception("ошибка обработки апдейта: %s", exc)
            await asyncio.sleep(0.4)

    async def alert_loop(self, interval: float = 4.0) -> None:
        """Уведомления о новых сделках и о срабатывании риск-стопа."""
        while not self.engine.stopping:
            await asyncio.sleep(interval)
            pf = self.engine.portfolio
            new = pf.fills[self.seen_fills:]
            self.seen_fills = len(pf.fills)
            for fill in new:
                if fill.pnl:
                    icon = "✅" if fill.pnl > 0 else "❌"
                    text = (f"{icon} <b>Выход</b> · {esc(fill.strategy)}\n"
                            f"{esc(pb.short(fill.question, 60))}\n"
                            f"{fill.size:,.0f} × {fill.price:.3f} → <b>{pb.money(fill.pnl)}</b>")
                else:
                    text = (f"🟦 <b>Вход</b> · {esc(fill.strategy)}\n"
                            f"{esc(pb.short(fill.question, 60))}\n"
                            f"{fill.size:,.0f} × {fill.price:.3f} = "
                            f"${fill.size * fill.price:,.0f}")
                await self.tg.send(self.owner, text)

            blocked = self.engine.risk.blocked_reason
            if blocked != self.last_blocked:
                self.last_blocked = blocked
                if blocked:
                    await self.tg.send(self.owner, f"🛑 <b>Риск-стоп:</b> {esc(blocked)}\n"
                                                   "Новые входы отключены.")
                else:
                    await self.tg.send(self.owner, "🟢 Риск-стоп снят, торгую дальше.")

    async def panel_loop(self, interval: float = 8.0) -> None:
        """Держим живую панель свежей, пока она открыта."""
        while not self.engine.stopping:
            await asyncio.sleep(interval)
            if self.panel_msg:
                await self.tg.edit(self.owner, self.panel_msg, render_panel(self.engine),
                                   panel_markup(self.engine, self.public_url))


# --------------------------------------------------------------------------------------
# Mini App: веб-сервер
# --------------------------------------------------------------------------------------
def build_web_app(engine: pb.Engine, token: str, owner: str) -> web.Application:
    index_file = WEBAPP_DIR / "index.html"

    async def index(_: web.Request) -> web.StreamResponse:
        if not index_file.exists():
            return web.Response(text="webapp/index.html не найден", status=500)
        return web.Response(text=index_file.read_text(encoding="utf-8"),
                            content_type="text/html")

    def authorize(payload: dict[str, Any]) -> tuple[bool, str]:
        data = check_init_data(str(payload.get("initData", "")), token)
        if not data:
            return False, "подпись Telegram не сошлась"
        user_id = str((data.get("user") or {}).get("id", ""))
        if user_id != str(owner):
            return False, "эта панель принадлежит другому аккаунту"
        return True, ""

    async def state(request: web.Request) -> web.StreamResponse:
        payload = await request.json()
        ok, reason = authorize(payload)
        if not ok:
            return web.json_response({"error": reason}, status=403)
        return web.json_response(state_dict(engine))

    async def action(request: web.Request) -> web.StreamResponse:
        payload = await request.json()
        ok, reason = authorize(payload)
        if not ok:
            return web.json_response({"error": reason}, status=403)
        what = str(payload.get("action", ""))
        if what == "pause":
            engine.paused = True
        elif what == "resume":
            engine.paused = False
        elif what == "flat":
            await engine.flatten("кнопка в Mini App")
        elif what == "close":
            token_id = str(payload.get("token_id", ""))
            book = await engine.data.book(token_id)
            engine._close(token_id, book, "закрытие из Mini App")
        else:
            return web.json_response({"error": "неизвестное действие"}, status=400)
        return web.json_response(state_dict(engine))

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/api/state", state)
    app.router.add_post("/api/action", action)
    return app


# --------------------------------------------------------------------------------------
# Запуск
# --------------------------------------------------------------------------------------
async def main_async(args: argparse.Namespace) -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    owner = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not owner:
        print("Нужны TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env.\n"
              "Токен — у @BotFather, свой chat_id — у @userinfobot.")
        return 1

    cfg = pb.Config.from_env()
    cfg.live = args.live
    if args.bankroll:
        cfg.bankroll = args.bankroll

    public_url = os.environ.get("WEBAPP_PUBLIC_URL", "").strip().rstrip("/")
    host = os.environ.get("WEBAPP_HOST", "0.0.0.0")
    port = pb.env_int("WEBAPP_PORT", 8080)

    async with pb.Http(cfg.max_concurrency) as http:
        store = pb.Store(pb.DB_PATH)
        engine = pb.Engine(cfg, http, store)
        tg = Telegram(token, http)
        bot = TgBot(engine, tg, owner, public_url if not args.no_web else "")

        runner: web.AppRunner | None = None
        if not args.no_web:
            runner = web.AppRunner(build_web_app(engine, token, owner))
            await runner.setup()
            await web.TCPSite(runner, host, port).start()
            log.info("Mini App слушает http://%s:%s", host, port)
            if not public_url:
                log.warning("WEBAPP_PUBLIC_URL пуст — кнопка панели скрыта, "
                            "команды бота работают")

        mode = "БОЕВОЙ" if cfg.live else "бумажный"
        await tg.send(owner, f"🚀 <b>Polybot запущен</b> · режим {mode}\n"
                             f"банк ${cfg.bankroll:,.0f}\n\n/panel — живая панель, /help — команды",
                      panel_markup(engine, bot.public_url))

        tasks = [
            asyncio.create_task(engine.run(None, once=False)),
            asyncio.create_task(bot.poll_loop()),
            asyncio.create_task(bot.alert_loop()),
            asyncio.create_task(bot.panel_loop()),
        ]
        try:
            await asyncio.gather(*tasks)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            engine.request_stop()
            for task in tasks:
                task.cancel()
            if runner:
                await runner.cleanup()
            store.close()
            await tg.send(owner, "⏹ Polybot остановлен.\n\n" + render_status(engine))
    return 0


def main() -> int:
    pb.load_dotenv(BASE_DIR / ".env")
    parser = argparse.ArgumentParser(description="Polybot в Telegram: бот + Mini App")
    parser.add_argument("--live", action="store_true", help="боевые ордера (см. README)")
    parser.add_argument("--no-web", action="store_true", help="без Mini App, только команды")
    parser.add_argument("--bankroll", type=float, help="переопределить банк в USDC")
    args = parser.parse_args()

    pb.setup_logging(quiet=False)
    if args.live and os.environ.get("POLYBOT_CONFIRM_LIVE", "") != "yes":
        print("Боевой режим выключен предохранителем.\n"
              "Поставь POLYBOT_CONFIRM_LIVE=yes в .env, если готов торговать реальными деньгами.")
        return 1
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
