#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Самопроверка Polybot на синтетическом рынке — без сети и без денег.

Подменяет HTTP-слой генератором данных (Gamma / CLOB / Data API) и прогоняет весь путь:
фильтры вселенной → индикаторы → сигналы → риск-менеджер → бумажное исполнение →
переоценка → выход по тейк-профиту → учёт P&L.

Запуск:
    python selftest.py
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import sys
from typing import Any

import polybot as pb

random.seed(7)


class FakeApi:
    """Синтетический Polymarket: 6 рынков, у каждого своя ценовая траектория."""

    def __init__(self, n_markets: int = 6) -> None:
        self.errors = 0
        self.requests = 0
        self.last_error = ""
        self.phase = 0.0                     # сдвигаем — цены «оживают» между циклами
        self.tokens: dict[str, int] = {}
        self.markets: list[dict[str, Any]] = []
        end = pb.now() + 20 * 86400
        for i in range(n_markets):
            token = f"tok{i}"
            self.tokens[token] = i
            self.markets.append({
                "id": str(100 + i),
                "conditionId": f"cond{i}",
                "question": f"Тестовый рынок №{i}: сработает ли стратегия?",
                "slug": f"test-market-{i}",
                "clobTokenIds": json.dumps([token, f"{token}-no"]),
                "outcomes": json.dumps(["Yes", "No"]),
                "outcomePrices": json.dumps([f"{self.price(token, 0):.3f}", "0.5"]),
                "liquidity": 50_000,
                "volume24hr": 250_000,
                "endDate": pb.datetime.fromtimestamp(end, pb.timezone.utc).isoformat(),
                "enableOrderBook": True,
            })

    # цена: медленная синусоида + провал, чтобы сработали RSI/z-score
    def price(self, token: str, step: int) -> float:
        if token.endswith("-no"):                      # парный исход стоит 1 - p
            return round(1.0 - self.price(token[:-3], step), 4)
        idx = self.tokens.get(token, 0)
        base = 0.45 + 0.05 * idx / 6
        wave = 0.06 * math.sin((step + idx * 7) / 9.0)
        dip = -0.10 * math.exp(-((step - 95) ** 2) / 60.0)
        return pb.clamp(base + wave + dip + self.phase, 0.05, 0.95)

    async def get_json(self, url: str, params: dict[str, Any] | None = None,
                       attempts: int = 3) -> Any:
        self.requests += 1
        params = params or {}
        if url.endswith("/markets"):
            return self.markets
        if url.endswith("/book"):
            price = self.price(str(params.get("token_id")), 100)
            return {
                "bids": [{"price": f"{price - 0.01 * k - 0.005:.3f}", "size": "500"}
                         for k in range(5)],
                "asks": [{"price": f"{price + 0.01 * k + 0.005:.3f}", "size": "500"}
                         for k in range(5)],
            }
        if url.endswith("/prices-history"):
            token = str(params.get("market"))
            t0 = pb.now() - 100 * 300
            return {"history": [{"t": t0 + s * 300, "p": round(self.price(token, s), 4)}
                                for s in range(101)]}
        if url.endswith("/trades"):
            cond = str(params.get("market", "cond0"))
            token = f"tok{cond.replace('cond', '')}"
            base = pb.now() - 3 * 3600
            out = []
            for s in range(120):
                price = self.price(token, 40 + s // 2)
                # к концу окна перевес покупок — VWAP выше цены, CVD растёт
                side = "BUY" if (s > 70 or random.random() > 0.45) else "SELL"
                out.append({"timestamp": base + s * 90, "price": round(price + 0.02, 4),
                            "size": round(random.uniform(20, 300), 2), "side": side})
            return out
        raise AssertionError(f"неожиданный запрос: {url}")


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = f"{pb.C.GREEN}OK  {pb.C.RESET}" if condition else f"{pb.C.RED}FAIL{pb.C.RESET}"
    print(f"  {mark} {label}{('  — ' + detail) if detail else ''}")
    return condition


def test_indicators() -> bool:
    ok = True
    rising = [0.30 + i * 0.01 for i in range(40)]
    falling = [0.70 - i * 0.01 for i in range(40)]
    ok &= check("RSI на росте > 70", (pb.rsi(rising) or 0) > 70, f"{pb.rsi(rising):.1f}")
    down = pb.rsi(falling)
    ok &= check("RSI на падении < 30", down is not None and down < 30, f"{down:.1f}")
    ok &= check("RSI без данных → None", pb.rsi([0.5, 0.5]) is None)

    trades = [pb.Trade(pb.now() - 60, 0.40, 100, "BUY"),
              pb.Trade(pb.now() - 30, 0.60, 300, "SELL")]
    value = pb.vwap(trades) or 0
    ok &= check("VWAP взвешен по объёму", abs(value - 0.55) < 1e-6, f"{value:.4f}")

    buys = [pb.Trade(pb.now() - 100 + i, 0.5, 10, "BUY") for i in range(20)]
    ok &= check("CVD растёт на покупках", pb.cvd_series(buys)[-1] > 0)

    flat = [0.5] * 40 + [0.62]
    z = pb.zscore(flat) or 0
    ok &= check("z-score ловит выброс", z > 3, f"z={z:.1f}")
    return bool(ok)


def test_paper_broker() -> bool:
    cfg = pb.Config()
    cfg.slippage_ticks = 0
    broker = pb.PaperBroker(cfg)
    book = pb.Book(bids=[(0.49, 100), (0.48, 100)], asks=[(0.51, 100), (0.52, 100)])

    result = broker.execute("BUY", book, 20.0)
    ok = check("покупка проходит по лучшему аску", result is not None)
    if result:
        shares, price, _ = result
        ok &= check("цена входа = лучший аск", abs(price - 0.51) < 1e-9, f"{price:.4f}")
        ok &= check("объём считается верно", abs(shares - 20 / 0.51) < 1e-6, f"{shares:.2f}")

    deep = broker.execute("BUY", book, 100.0)   # съедаем первый уровень, идём во второй
    if deep:
        _, price, _ = deep
        ok &= check("проход по уровням даёт среднюю цену хуже лучшей", price > 0.51,
                    f"{price:.4f}")
    ok &= check("тонкий стакан → входа нет",
                broker.execute("BUY", pb.Book(asks=[(0.51, 1)], bids=[(0.49, 1)]), 500.0) is None)
    return bool(ok)


def test_risk() -> bool:
    cfg = pb.Config()
    cfg.bankroll = 1000
    risk = pb.RiskManager(cfg)
    market = pb.Market("1", "c1", "q", "s", "tok", "Yes", "tok-no", "No",
                       0.5, 1e5, 1e5, pb.now() + 86400)
    good = pb.Signal("test", market, "BUY", 0.60, 0.50, 0.09, 0.9, "")
    weak = pb.Signal("test", market, "BUY", 0.51, 0.50, 0.005, 0.2, "")

    size_good = risk.size_for(good, 1000, 0)
    size_weak = risk.size_for(weak, 1000, 0)
    ok = check("размер не больше риска на сделку", size_good <= 1000 * cfg.risk_per_trade + 1e-9,
               f"${size_good:.2f}")
    ok &= check("слабый сигнал получает меньше сильного", size_weak < size_good,
                f"${size_weak:.2f} < ${size_good:.2f}")
    ok &= check("лимит позиций закрывает вход", risk.size_for(good, 1000, cfg.max_positions) == 0)
    ok &= check("дневной лимит убытка блокирует торговлю",
                bool(risk.check_portfolio(920, 1000, 1000)))
    ok &= check("в норме блокировки нет", risk.check_portfolio(1010, 1010, 1000) == "")
    return bool(ok)


async def test_engine() -> bool:
    cfg = pb.Config()
    cfg.bankroll = 1000
    cfg.scan_interval = 0.0          # сканируем каждый цикл
    cfg.min_edge = 0.015
    api = FakeApi()
    engine = pb.Engine(cfg, api, store=None)   # type: ignore[arg-type]

    await engine.scan()
    ok = check("вселенная собрана", len(engine.universe) == 6, f"{len(engine.universe)} рынков")
    ok &= check("снимки получены", len(engine.snapshots) == 6, f"{len(engine.snapshots)}")

    signals = engine.signals()
    ok &= check("стратегии дают сигналы", len(signals) > 0, f"{len(signals)} шт")
    ok &= check("сигналы отсортированы по edge×confidence",
                all(a.edge * a.confidence >= b.edge * b.confidence
                    for a, b in zip(signals, signals[1:])))

    await engine.open_positions(signals)
    pf = engine.portfolio
    ok &= check("позиции открыты", len(pf.positions) > 0, f"{len(pf.positions)}")
    ok &= check("лимит позиций соблюдён", len(pf.positions) <= cfg.max_positions)
    ok &= check("кэш уменьшился на стоимость входов",
                abs((cfg.bankroll - pf.cash) - sum(p.cost for p in pf.positions.values())) < 0.01)
    ok &= check("баланс сходится: кэш + экспозиция = эквити",
                abs(pf.cash + pf.exposure - pf.equity) < 1e-6)

    opened = len(pf.positions)
    api.phase = 0.12                 # рынок вырос — должен сработать тейк-профит
    await engine.manage_positions()
    ok &= check("тейк-профит закрыл позиции", len(pf.positions) < opened,
                f"осталось {len(pf.positions)}")
    ok &= check("прибыль зафиксирована", pf.realized > 0, pb.money(pf.realized))
    ok &= check("win rate посчитан", pf.win_rate > 0, f"{pf.win_rate*100:.0f}%")
    ok &= check("эквити выросла", pf.equity > cfg.bankroll, f"${pf.equity:,.2f}")

    api.phase = -0.25                # обвал — оставшееся должно уйти по стоп-лоссу
    await engine.manage_positions()
    ok &= check("стоп-лосс отрабатывает", all(p.upnl() > -cfg.stop_loss * p.size * 1.5
                                              for p in pf.positions.values()))

    ok &= check("кривая эквити пишется", len(pf.equity_curve) > 2, f"{len(pf.equity_curve)} точек")
    ok &= check("метрики считаются без ошибок",
                isinstance(pf.sharpe, float) and isinstance(pf.max_drawdown, float),
                f"sharpe {pf.sharpe:.2f}, maxDD {pf.max_drawdown*100:.1f}%")
    return bool(ok)


async def test_short_side() -> bool:
    """Сигнал на продажу YES должен превращаться в покупку парного токена NO."""
    cfg = pb.Config()
    cfg.bankroll = 1000
    cfg.min_edge = 0.01
    api = FakeApi()
    engine = pb.Engine(cfg, api, store=None)   # type: ignore[arg-type]
    await engine.scan()

    snap = next(iter(engine.snapshots.values()))
    market = snap.market
    # YES дорог и «должен» упасть → fair ниже цены; для NO это перевес вверх
    signal = pb.Signal("test", market, "SELL", fair=snap.mid - 0.08,
                       price=snap.book.best_bid, edge=0.08, confidence=0.9, reason="тест")
    await engine.open_positions([signal])

    pf = engine.portfolio
    ok = check("позиция открыта по сигналу на продажу", len(pf.positions) == 1)
    if pf.positions:
        pos = next(iter(pf.positions.values()))
        ok &= check("куплен именно парный токен NO", pos.token_id == market.no_token_id,
                    pos.token_id)
        ok &= check("исход в позиции — No", pos.outcome == market.no_outcome, pos.outcome)
        ok &= check("цена входа около 1 - цены YES", abs(pos.entry - (1 - snap.mid)) < 0.05,
                    f"{pos.entry:.3f} против {1 - snap.mid:.3f}")

    before = len(pf.positions)
    await engine.open_positions([signal])       # тот же рынок второй раз брать нельзя
    ok &= check("повторный вход в тот же рынок отклонён", len(pf.positions) == before)
    return bool(ok)


async def test_telegram() -> bool:
    """Telegram-слой: подпись Mini App, JSON состояния, тексты сообщений."""
    import hashlib
    import hmac
    import json as _json
    import urllib.parse

    import tgapp

    token = "123456:TEST-TOKEN"

    def sign(fields: dict[str, str]) -> str:
        check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return urllib.parse.urlencode({**fields, "hash": digest})

    fields = {"auth_date": str(int(pb.now())), "query_id": "AAA",
              "user": _json.dumps({"id": 777, "first_name": "Тест"}, ensure_ascii=False)}
    good = sign(fields)

    ok = check("валидная подпись принимается",
               (tgapp.check_init_data(good, token) or {}).get("user", {}).get("id") == 777)
    ok &= check("подделанные данные отклоняются",
                tgapp.check_init_data(good.replace("AAA", "BBB"), token) is None)
    ok &= check("чужой токен бота отклоняется",
                tgapp.check_init_data(good, "999:OTHER") is None)
    ok &= check("протухшая подпись отклоняется",
                tgapp.check_init_data(sign({**fields, "auth_date": str(int(pb.now() - 90000))}),
                                      token) is None)
    ok &= check("пустая строка отклоняется", tgapp.check_init_data("", token) is None)

    cfg = pb.Config()
    api = FakeApi()
    engine = pb.Engine(cfg, api, store=None)     # type: ignore[arg-type]
    await engine.scan()
    await engine.open_positions(engine.signals())

    state = tgapp.state_dict(engine)
    ok &= check("состояние сериализуется в JSON",
                bool(_json.dumps(state)), f"{len(state['positions'])} позиций")
    ok &= check("баланс в состоянии сходится",
                abs(state["cash"] + state["exposure"] - state["equity"]) < 0.02)
    ok &= check("лимиты уехали в панель", state["limits"]["max_positions"] == cfg.max_positions)
    ok &= check("тикер собран по рынкам в работе",
                len(state["ticker"]) == len(engine.snapshots),
                f"{len(state['ticker'])} строк")
    ok &= check("в тикере есть цена и движение",
                all(0 < t["price"] < 1 and "change" in t for t in state["ticker"]))
    ok &= check("тикер помечает рынки с позицией",
                sum(1 for t in state["ticker"] if t["held"]) == len(engine.portfolio.positions))
    ok &= check("ярлык тикера короткий и без служебных слов",
                all(t["label"] and len(t["label"]) <= 18 and " " not in t["label"]
                    for t in state["ticker"]),
                ", ".join(t["label"] for t in state["ticker"][:3]))
    ok &= check("лента событий пишется", len(state["log"]) > 0, f"{len(state['log'])} записей")
    ok &= check("вход попал в ленту",
                any(e["level"] == "buy" for e in state["log"]))
    ok &= check("лента отсортирована свежим вперёд",
                all(a["ts"] >= b["ts"] for a, b in zip(state["log"], state["log"][1:])))

    pf = engine.portfolio
    pf.cash += 40.0                                   # изображаем зафиксированную прибыль
    pf.day_start_equity = pf.equity - 12.5
    money = tgapp.render_pnl(engine)
    ok &= check("/pnl показывает плюс", "В плюсе" in money, pb.short(money.splitlines()[0], 40))
    ok &= check("/pnl считает результат за сегодня", "сегодня" in money)
    ok &= check("/pnl помечает бумажный режим", "Бумажный режим" in money)
    ok &= check("склонение «сделка» по-русски",
                [tgapp.plural(n, ("сделка", "сделки", "сделок")) for n in (1, 2, 5, 11, 21, 24)]
                == ["сделка", "сделки", "сделок", "сделок", "сделка", "сделки"])
    pf.cash -= 300.0
    ok &= check("/pnl показывает минус", "В минусе" in tgapp.render_pnl(engine))
    pf.cash += 260.0

    texts = [tgapp.render_status(engine), tgapp.render_positions(engine),
             tgapp.render_trades(engine), tgapp.render_stats(engine),
             tgapp.render_panel(engine), tgapp.render_pnl(engine)]
    ok &= check("все сообщения рендерятся", all(texts))
    ok &= check("влезают в лимит Telegram (4096)", all(len(t) < 4096 for t in texts),
                f"максимум {max(len(t) for t in texts)} симв.")
    ok &= check("HTML-теги сбалансированы",
                all(t.count("<b>") == t.count("</b>") and t.count("<i>") == t.count("</i>")
                    for t in texts))

    engine.paused = True
    ok &= check("пауза блокирует новые входы", "ПАУЗА" in engine.status_line())
    before = len(engine.portfolio.positions)
    await engine.open_positions(engine.signals())
    ok &= check("на паузе позиции не открываются", len(engine.portfolio.positions) == before)
    engine.paused = False

    closed = await engine.flatten("тест")
    ok &= check("flatten закрывает всё", closed == before and not engine.portfolio.positions,
                f"закрыто {closed}")

    bot = tgapp.TgBot(engine, tgapp.Telegram(token, api), owner="", public_url="")  # type: ignore[arg-type]
    ok &= check("без владельца бот никого не слушает", not bot.is_owner("555"))
    bot.claim("555")
    ok &= check("первый /start назначает владельца", bot.is_owner("555"))
    ok &= check("после этого чужой чат отсекается", not bot.is_owner("556"))

    markup = tgapp.panel_markup(engine, "https://example.com/app")
    buttons = [b for row in markup["inline_keyboard"] for b in row]
    ok &= check("кнопка Mini App появляется при https-адресе",
                any("web_app" in b for b in buttons))
    ok &= check("без адреса кнопки Mini App нет",
                not any("web_app" in b for row in tgapp.panel_markup(engine, "")["inline_keyboard"]
                        for b in row))
    return bool(ok)


def test_launcher() -> bool:
    """Запускалка start.py: распознавание токена и запись в .env без потери строк."""
    import tempfile

    import start

    ok = check("настоящий токен принимается",
               start.valid_token("123456789:AAFakeTokenForTestsOnly-0123456789abc"))
    ok &= check("мусор вместо токена отклоняется",
                not any(start.valid_token(x) for x in
                        ("непонятночто", "", "123", "12345:short", "--live",
                         "123456789 AAFakeTokenForTestsOnly-0123456789abc")))
    ok &= check("флаги не принимаются за токен", not start.valid_token("--terminal"))

    with tempfile.TemporaryDirectory() as tmp:
        env = pb.Path(tmp) / ".env"
        env.write_text("BANKROLL=500\nTELEGRAM_BOT_TOKEN=\nMAX_POSITIONS=4\n", encoding="utf-8")
        original = start.ENV_FILE
        try:
            start.ENV_FILE = env
            start.save_token("111111:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
            text = env.read_text(encoding="utf-8")
        finally:
            start.ENV_FILE = original
    ok &= check("токен записан в .env",
                "TELEGRAM_BOT_TOKEN=111111:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" in text)
    ok &= check("остальные настройки не пострадали",
                "BANKROLL=500" in text and "MAX_POSITIONS=4" in text)
    ok &= check("строка токена не задвоилась", text.count("TELEGRAM_BOT_TOKEN=") == 1)
    return bool(ok)


def test_dashboard() -> bool:
    cfg = pb.Config()
    api = FakeApi()
    engine = pb.Engine(cfg, api, store=None)     # type: ignore[arg-type]
    engine.portfolio.positions["tok0"] = pb.Position(
        "tok0", "Демо-позиция для кадра дашборда", "Да", "rsi_vwap",
        100, 0.42, pb.now(), 0.48, mark=0.45)
    engine.portfolio.fills.append(pb.Fill(pb.now(), "tok0", "Демо-сделка", "BUY",
                                          100, 0.42, 0.0, "rsi_vwap"))
    engine.portfolio.equity_curve = [1000 + i * 0.7 + random.uniform(-2, 2) for i in range(120)]
    engine.strategies[0].note("BUY демо @ 0.42 edge +3.1c")

    dash = pb.Dashboard(engine)
    dash.enabled = True
    lines: list[str] = []
    for block in (dash._header, dash._strategies, dash._positions,
                  dash._executions, dash._equity, dash._footer):
        lines += block(120)
    ok = check("кадр собирается целиком", len(lines) >= 18, f"{len(lines)} строк")
    ok &= check("строки влезают в ширину терминала",
                all(len(pb.strip_ansi(line)) <= 121 for line in lines))
    ok &= check("спарклайн рисуется", bool(pb.sparkline(engine.portfolio.equity_curve, 80)))
    print("\n" + "\n".join(lines) + "\n")
    return bool(ok)


async def main() -> int:
    print(f"{pb.C.BOLD}Самопроверка Polybot{pb.C.RESET} — синтетический рынок, сеть не нужна\n")
    results = {}
    print("Индикаторы:")
    results["индикаторы"] = test_indicators()
    print("\nБумажное исполнение:")
    results["исполнение"] = test_paper_broker()
    print("\nРиск-менеджмент:")
    results["риск"] = test_risk()
    print("\nДвижок (сканирование → сигналы → сделки → выходы):")
    results["движок"] = await test_engine()
    print("\nИгра против исхода (SELL YES = BUY NO):")
    results["short"] = await test_short_side()
    print("\nTelegram (подпись Mini App, состояние, сообщения):")
    results["telegram"] = await test_telegram()
    print("\nЗапуск одной командой (start.py):")
    results["запуск"] = test_launcher()
    print("\nДашборд:")
    results["дашборд"] = test_dashboard()

    failed = [name for name, ok in results.items() if not ok]
    if failed:
        print(f"{pb.C.RED}Провалено: {', '.join(failed)}{pb.C.RESET}")
        return 1
    print(f"{pb.C.GREEN}Все проверки пройдены.{pb.C.RESET} "
          "Это проверка кода, а не доходности стратегий.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
