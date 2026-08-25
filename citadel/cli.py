# -*- coding: utf-8 -*-
"""Командная строка Citadel Trader."""
from __future__ import annotations

import argparse
import logging
import random
import sys

from .backtest import run_backtest
from .bot import Trader
from .broker import PaperBroker, make_broker
from .config import Config
from .evolve import evolve
from .features import build_features
from .genome import Genome
from .market import Market
from .notify import Notifier
from . import console
from .storage import Storage

log = logging.getLogger("citadel")

BANNER = r"""
   ____ _ _            _      _   _____             _
  / ___(_) |_ __ _  __| | ___| | |_   _|_ _ ___  __| | ___ _ __
 | |   | | __/ _` |/ _` |/ _ \ |   | |/ _` / _ \/ _` |/ _ \ '__|
 | |___| | || (_| | (_| |  __/ |   | | (_| | (_) | (_| |  __/ |
  \____|_|\__\__,_|\__,_|\___|_|   |_|\__,_\___/\__,_|\___|_|
     спот-бот, который сам выводит себе стратегию
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tradebot",
        description="Citadel Trader — бот, который сам ищет себе стратегию и торгует по ней на споте.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Примеры:\n"
               "  python tradebot.py fetch\n"
               "  python tradebot.py evolve --symbol BTC/USDT\n"
               "  python tradebot.py backtest --offline\n"
               "  python tradebot.py trade                # бумажная торговля\n"
               "  python tradebot.py trade --live         # реальные ордера\n")
    p.add_argument("--exchange", help="биржевой id из ccxt (по умолчанию binance)")
    p.add_argument("--symbols", help="список пар через запятую, напр. BTC/USDT,ETH/USDT")
    p.add_argument("--timeframe", help="таймфрейм: 15m, 1h, 4h, 1d")
    p.add_argument("--history", type=int, help="сколько свечей брать на обучение")
    p.add_argument("--offline", action="store_true", help="только кэш свечей, без сети")
    p.add_argument("--dry", action="store_true", help="не отправлять уведомления в Telegram")
    p.add_argument("--db", help="путь к файлу базы")
    p.add_argument("-v", "--verbose", action="store_true", help="подробный лог")

    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch", help="скачать и закэшировать свечи")

    ev = sub.add_parser("evolve", help="найти стратегию (генетический поиск)")
    ev.add_argument("--symbol", help="только по одной паре")
    ev.add_argument("--population", type=int)
    ev.add_argument("--generations", type=int)
    ev.add_argument("--seed", type=int)

    bt = sub.add_parser("backtest", help="прогнать активную стратегию по истории")
    bt.add_argument("--symbol")
    bt.add_argument("--strategy", type=int, help="id стратегии из базы")
    bt.add_argument("--trades", action="store_true", help="показать список сделок")

    tr = sub.add_parser("trade", help="торговать (по умолчанию — бумажный счёт)")
    tr.add_argument("--live", action="store_true", help="реальные ордера на бирже")
    tr.add_argument("--once", action="store_true", help="один круг и выход")
    tr.add_argument("--yes", action="store_true", help="не спрашивать подтверждение для --live")
    tr.add_argument("--dashboard", nargs="?", const="", metavar="ФАЙЛ",
                    help="обновлять HTML-страницу состояния во время торговли")

    pn = sub.add_parser("pine", help="выгрузить стратегию в Pine Script для TradingView")
    pn.add_argument("--symbol")
    pn.add_argument("--strategy", type=int, help="id стратегии из базы")
    pn.add_argument("--trades", action="store_true",
                    help="ещё и оверлей с реальными сделками бота")
    pn.add_argument("--out", help="каталог для .pine файлов (по умолчанию data/pine)")

    db = sub.add_parser("dashboard", help="собрать HTML-страницу с состоянием (открывается двойным кликом)")
    db.add_argument("--out", help="куда сохранить файл (по умолчанию data/dashboard.html)")
    db.add_argument("--open", action="store_true", help="сразу открыть в браузере")
    db.add_argument("--refresh", type=int, default=0,
                    help="страница будет сама перезагружаться раз в N секунд")

    sub.add_parser("report", help="состояние счёта, позиции, стратегии")
    rs = sub.add_parser("reset", help="сбросить бумажный счёт и позиции")
    rs.add_argument("--yes", action="store_true")
    return p


def apply_overrides(cfg: Config, a: argparse.Namespace) -> Config:
    if a.exchange:
        cfg.exchange = a.exchange
    if a.symbols:
        cfg.symbols = tuple(s.strip().upper() for s in a.symbols.split(",") if s.strip())
    if a.timeframe:
        cfg.timeframe = a.timeframe
    if a.history:
        cfg.history = a.history
    if a.db:
        cfg.db_path = a.db
    for name in ("population", "generations", "seed"):
        val = getattr(a, name, None)
        if val:
            setattr(cfg, name, val)
    if getattr(a, "symbol", None):
        cfg.symbols = (a.symbol.strip().upper(),)
    cfg.live = bool(getattr(a, "live", False))
    return cfg


def main(argv: list[str] | None = None) -> int:
    console.setup()          # windows-консоль иначе падает на эмодзи
    args = build_parser().parse_args(argv)
    cfg = apply_overrides(Config.from_env(), args)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")

    cfg.ensure_dirs()
    store = Storage(cfg.db_path)
    notifier = Notifier(cfg.telegram_token, cfg.telegram_chat_id, enabled=not args.dry)

    if args.cmd == "reset":
        return cmd_reset(cfg, store, args)

    market = Market(cfg, need_keys=cfg.live, offline=args.offline)

    if args.cmd == "fetch":
        return cmd_fetch(cfg, market)
    if args.cmd == "evolve":
        return cmd_evolve(cfg, store, market, args)
    if args.cmd == "backtest":
        return cmd_backtest(cfg, store, market, args)
    if args.cmd == "pine":
        return cmd_pine(cfg, store, args)
    if args.cmd == "dashboard":
        return cmd_dashboard(cfg, store, args, mode="cex")
    if args.cmd == "report":
        trader = Trader(cfg, store, market, make_broker(cfg, store, market), notifier, args.offline)
        print(trader.report().replace("<b>", "").replace("</b>", ""))
        return 0
    if args.cmd == "trade":
        return cmd_trade(cfg, store, market, notifier, args)
    return 1


# ── команды ─────────────────────────────────────────────────────────────────
def cmd_fetch(cfg: Config, market: Market) -> int:
    for symbol in cfg.symbols:
        c = market.fetch_ohlcv(symbol, cfg.timeframe, cfg.history)
        print(f"{symbol}: {len(c)} свечей, кэш → {market.cache_path(symbol, cfg.timeframe)}")
    return 0


def cmd_evolve(cfg: Config, store: Storage, market: Market, args) -> int:
    print(BANNER)
    for symbol in cfg.symbols:
        candles = market.fetch_ohlcv(symbol, cfg.timeframe, cfg.history, offline=args.offline)
        f = build_features(candles)
        print(f"\n=== {symbol} · {cfg.timeframe} · {len(candles)} свечей ===")
        cand = evolve(f, cfg, random.Random(cfg.seed or None),
                      on_generation=lambda g, s, _: print(f"  поколение {g+1:>3}: фитнес {s:6.2f}", flush=True))
        if cand is None:
            print("  годной стратегии не нашлось — бот по этой паре торговать не будет")
            continue
        sid = store.save_strategy(symbol, cfg.timeframe, cand.genome, cand.valid_score, cand.as_meta())
        store.activate(sid, symbol)
        print(f"\n  стратегия #{sid}, скор на валидации {cand.valid_score:.2f}")
        print("  " + cand.genome.describe().replace("\n", "\n  "))
        print(f"  обучение:   {cand.train.summary()}")
        print(f"  валидация:  {cand.valid.summary()}")
    return 0


def cmd_backtest(cfg: Config, store: Storage, market: Market, args) -> int:
    rc = 0
    for symbol in cfg.symbols:
        if args.strategy:
            row = store.db.execute("SELECT * FROM strategies WHERE id=?", (args.strategy,)).fetchone()
        else:
            row = store.active_strategy(symbol)
        if not row:
            print(f"{symbol}: активной стратегии нет — сначала `python tradebot.py evolve`")
            rc = 1
            continue
        g = Genome.from_json(row["genome"])
        candles = market.fetch_ohlcv(symbol, cfg.timeframe, cfg.history, offline=args.offline)
        f = build_features(candles)
        res = run_backtest(f, g, cfg)
        print(f"\n=== {symbol} · стратегия #{row['id']} · {len(candles)} свечей ===")
        print(g.describe())
        print(res.summary())
        if args.trades:
            for t in res.trades:
                print(f"  {t.entry_i:>5} → {t.exit_i:<5} {t.entry_price:>12.6g} → {t.exit_price:<12.6g}"
                      f" {t.pnl:+10.2f} ({t.pnl_pct:+6.2f}%) {t.reason}")
    return rc


def cmd_pine(cfg: Config, store: Storage, args) -> int:
    from pathlib import Path

    from .pine import UnsupportedSignal, to_pine, trades_overlay, tv_symbol

    out_dir = Path(args.out) if args.out else Path(cfg.db_path).parent / "pine"
    out_dir.mkdir(parents=True, exist_ok=True)
    rc = 0
    for symbol in cfg.symbols:
        if args.strategy:
            row = store.db.execute("SELECT * FROM strategies WHERE id=?", (args.strategy,)).fetchone()
        else:
            row = store.active_strategy(symbol)
        if not row:
            print(f"{symbol}: активной стратегии нет — сначала `python tradebot.py evolve`")
            rc = 1
            continue
        g = Genome.from_json(row["genome"])
        try:
            src = to_pine(g, symbol, row["timeframe"], strategy_id=row["id"],
                          score=row["score"], capital=cfg.start_balance,
                          commission_pct=cfg.taker_fee * 100,
                          max_position_frac=cfg.max_position_frac, exchange=cfg.exchange)
        except UnsupportedSignal as e:
            print(f"{symbol}: стратегия #{row['id']} использует условие {e}, которого нет в "
                  f"переводе на Pine — перезапусти `evolve`, чтобы получить свежую стратегию")
            rc = 1
            continue
        safe = symbol.replace("/", "-")
        path = out_dir / f"citadel_{safe}_{row['timeframe']}_strategy{row['id']}.pine"
        path.write_text(src, encoding="utf-8")
        print(f"\n{symbol}: стратегия #{row['id']} → {path}")
        print(f"  в TradingView: открой график {tv_symbol(symbol, cfg.exchange)} на "
              f"{row['timeframe']}, Pine Editor → вставь файл → Add to chart")

        if args.trades:
            rows = store.db.execute(
                "SELECT * FROM trades WHERE symbol=? ORDER BY ts", (symbol,)).fetchall()
            buys = [(int(t["ts"]) * 1000, float(t["price"])) for t in rows if t["side"] == "buy"]
            sells = [(int(t["ts"]) * 1000, float(t["price"]), float(t["pnl"] or 0.0))
                     for t in rows if t["side"] == "sell"]
            if not buys and not sells:
                print("  сделок в базе пока нет — оверлей не создан")
            else:
                overlay = trades_overlay(symbol, row["timeframe"], buys, sells)
                tp = out_dir / f"citadel_{safe}_{row['timeframe']}_trades.pine"
                tp.write_text(overlay, encoding="utf-8")
                print(f"  сделки бота ({len(buys)} покупок, {len(sells)} продаж) → {tp}")
    return rc


def cmd_dashboard(cfg: Config, store: Storage, args, mode: str = "cex") -> int:
    from . import dashboard

    path = dashboard.write(cfg, store, args.out or None, mode, args.refresh)
    print(f"страница собрана: {path}")
    print("открой её двойным кликом — ничего запускать не нужно")
    if args.open:
        where = dashboard.open_in_browser(path)
        print(f"открываю: {where}" if where else "браузер не открылся — открой файл вручную")
    return 0


def cmd_trade(cfg: Config, store: Storage, market: Market, notifier: Notifier, args) -> int:
    if cfg.live:
        print("⚠️  РЕЖИМ РЕАЛЬНОЙ ТОРГОВЛИ: бот будет отправлять настоящие ордера на "
              f"{cfg.exchange} и тратить твои деньги.")
        if not args.yes:
            if input("Напиши YES, если согласен: ").strip() != "YES":
                print("Отменено.")
                return 1
    broker = make_broker(cfg, store, market)
    trader = Trader(cfg, store, market, broker, notifier, args.offline)
    if args.dashboard is not None:
        from pathlib import Path as _Path

        from . import dashboard as _dash

        trader.dashboard_path = _Path(args.dashboard) if args.dashboard else (
            _Path(cfg.db_path).parent / "dashboard.html")
        print(f"страница состояния: {trader.dashboard_path} (обновляется раз в минуту)")
        _dash.write(cfg, store, trader.dashboard_path, "cex", refresh_seconds=30)
    print(BANNER)
    print(f"режим: {broker.name} · {cfg.exchange} · {', '.join(cfg.symbols)} · {cfg.timeframe}")
    try:
        trader.run(once=args.once)
    except KeyboardInterrupt:
        print("\nостановлено пользователем")
    return 0


def cmd_reset(cfg: Config, store: Storage, args) -> int:
    if not args.yes and input("Сбросить бумажный счёт, позиции и сделки? (yes) ").strip() != "yes":
        print("Отменено.")
        return 1
    store.db.executescript("DELETE FROM positions; DELETE FROM trades; DELETE FROM equity;"
                           " DELETE FROM state WHERE k IN ('paper_cash','equity_peak','day',"
                           "'paused_reason','paper_start');")
    store.db.commit()
    PaperBroker(cfg, store)
    print(f"Счёт сброшен: {cfg.start_balance:.2f} {cfg.quote}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
