# -*- coding: utf-8 -*-
"""Командная строка DEX-бота."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from ..backtest import run_backtest
from ..features import build_features
from ..genome import Genome
from ..notify import Notifier
from ..storage import Storage
from .bot import DexTrader
from .broker import DexPaperBroker, make_dex_broker
from .config import DexConfig
from .market import DexMarket
from .safety import RugCheck, screen

log = logging.getLogger("citadel.dex")

BANNER = r"""
   ____ _ _            _      _   ____  _______  __
  / ___(_) |_ __ _  __| | ___| | |  _ \| ____\ \/ /
 | |   | | __/ _` |/ _` |/ _ \ | | | | |  _|  \  /
 | |___| | || (_| | (_| |  __/ | | |_| | |___ /  \
  \____|_|\__\__,_|\__,_|\___|_| |____/|_____/_/\_\
   DexScreener + GeckoTerminal + Jupiter · Solana и другие сети
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dexbot",
        description="Citadel DEX — тот же бот со своей стратегией, но на децентрализованных биржах.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Порядок работы:\n"
               "  python dexbot.py discover     # подобрать пары через DexScreener\n"
               "  python dexbot.py evolve       # найти стратегию под каждую пару\n"
               "  python dexbot.py trade        # торговать на бумажном счёте\n"
               "  python dexbot.py trade --live # реальные свопы через Jupiter\n")
    p.add_argument("--chain", help="сеть: solana, base, bsc, ethereum…")
    p.add_argument("--symbols", help="конкретные пулы через запятую: chain:адрес")
    p.add_argument("--timeframe", help="5m, 15m, 1h, 4h, 1d")
    p.add_argument("--history", type=int, help="сколько свечей брать")
    p.add_argument("--offline", action="store_true", help="только кэш, без сети")
    p.add_argument("--dry", action="store_true", help="не слать уведомления в Telegram")
    p.add_argument("--db", help="путь к базе")
    p.add_argument("-v", "--verbose", action="store_true")

    sub = p.add_subparsers(dest="cmd", required=True)
    dc = sub.add_parser("discover", help="подобрать пары: DexScreener + фильтры безопасности")
    dc.add_argument("--show-rejected", action="store_true", help="показать, кого и почему отсеяли")
    sub.add_parser("pairs", help="текущий список пар и их состояние")
    sub.add_parser("fetch", help="скачать свечи по выбранным парам")

    ev = sub.add_parser("evolve", help="найти стратегию под каждую пару")
    ev.add_argument("--population", type=int)
    ev.add_argument("--generations", type=int)
    ev.add_argument("--seed", type=int)

    bt = sub.add_parser("backtest", help="прогнать активные стратегии по истории")
    bt.add_argument("--trades", action="store_true")

    tr = sub.add_parser("trade", help="торговать (по умолчанию — бумажный счёт)")
    tr.add_argument("--live", action="store_true", help="реальные свопы через Jupiter")
    tr.add_argument("--once", action="store_true")
    tr.add_argument("--yes", action="store_true")

    pn = sub.add_parser("pine", help="выгрузить стратегии в Pine Script")
    pn.add_argument("--out")

    sub.add_parser("report", help="счёт, позиции, стратегии, сделки")
    rs = sub.add_parser("reset", help="сбросить бумажный счёт и сделки")
    rs.add_argument("--yes", action="store_true")
    return p


def apply_overrides(cfg: DexConfig, a: argparse.Namespace) -> DexConfig:
    if a.chain:
        cfg.chain = a.chain.lower()
    if a.symbols:
        cfg.symbols = tuple(s.strip() for s in a.symbols.split(",") if s.strip())
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
    cfg.live = bool(getattr(a, "live", False))
    return cfg


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = apply_overrides(DexConfig.from_env(), args)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    cfg.ensure_dirs()
    store = Storage(cfg.db_path)
    notifier = Notifier(cfg.telegram_token, cfg.telegram_chat_id, enabled=not args.dry)

    if args.cmd == "reset":
        return cmd_reset(cfg, store, args)

    market = DexMarket(cfg, offline=args.offline)
    if args.cmd == "discover":
        return cmd_discover(cfg, store, market, notifier, args)

    broker = make_dex_broker(cfg, store, market) if args.cmd == "trade" else \
        DexPaperBroker(cfg, store, market)
    trader = DexTrader(cfg, store, market, broker, notifier, args.offline)

    if args.cmd == "pairs":
        return cmd_pairs(trader)
    if args.cmd == "fetch":
        return cmd_fetch(trader)
    if args.cmd == "evolve":
        return cmd_evolve(trader)
    if args.cmd == "backtest":
        return cmd_backtest(trader, args)
    if args.cmd == "pine":
        return cmd_pine(trader, args)
    if args.cmd == "report":
        print(trader.report().replace("<b>", "").replace("</b>", ""))
        return 0
    if args.cmd == "trade":
        return cmd_trade(trader, args)
    return 1


# ── команды ─────────────────────────────────────────────────────────────────
def cmd_discover(cfg: DexConfig, store: Storage, market: DexMarket,
                 notifier: Notifier, args) -> int:
    print(BANNER)
    trader = DexTrader(cfg, store, market, DexPaperBroker(cfg, store, market),
                       notifier, args.offline)
    if args.show_rejected:
        raw = trader.candidates()
        print(f"кандидатов: {len(raw)}\n")
        for pair, bad in screen(raw, cfg.safety(), RugCheck()):
            mark = "✅" if not bad else "❌"
            print(f"{mark} {pair.describe()}")
            for reason in bad:
                print(f"     ↳ {reason}")
        print()
    universe = trader.discover()
    print(f"\nв работе {len(universe)} пар:")
    for key in universe:
        pair = market.pair(key)
        print(f"  • {pair.describe() if pair else key}\n    {key}")
    return 0 if universe else 1


def cmd_pairs(trader: DexTrader) -> int:
    if not trader.cfg.symbols:
        print("список пуст — запусти `python dexbot.py discover`")
        return 1
    for key in trader.cfg.symbols:
        pair = trader.market.pair(key)
        row = trader.store.active_strategy(key)
        strat = f"стратегия #{row['id']} (скор {row['score']:.2f})" if row else "стратегии нет"
        print(f"• {pair.describe() if pair else key}")
        print(f"    {key} · {strat}")
        if pair and pair.url:
            print(f"    {pair.url}")
    return 0


def cmd_fetch(trader: DexTrader) -> int:
    if not trader.cfg.symbols:
        print("список пар пуст — сначала `python dexbot.py discover`")
        return 1
    for key in trader.cfg.symbols:
        candles = trader.market.fetch_ohlcv(key, trader.cfg.timeframe, trader.cfg.history)
        print(f"{trader.market.name(key)}: {len(candles)} свечей → "
              f"{trader.market.cache_path(key, trader.cfg.timeframe)}")
    return 0


def cmd_evolve(trader: DexTrader) -> int:
    print(BANNER)
    if not trader.cfg.symbols:
        print("список пар пуст — сначала `python dexbot.py discover`")
        return 1
    found = 0
    for key in trader.cfg.symbols:
        print(f"\n=== {trader.market.name(key)} · {trader.cfg.timeframe} ===")
        if trader.train(key, force=True) is None:
            continue
        found += 1
        row = trader.store.active_strategy(key)
        if row:
            meta = json.loads(row["metrics"] or "{}")
            print(f"  обучение:  {fmt_metrics(meta.get('train'))}")
            print(f"  валидация: {fmt_metrics(meta.get('valid'))}")
    print(f"\nстратегий найдено: {found} из {len(trader.cfg.symbols)}")
    return 0 if found else 1


def fmt_metrics(m: dict | None) -> str:
    if not m:
        return "нет данных"
    pf = m.get("profit_factor") or 0.0
    return (f"сделок {m.get('n_trades', 0)} | доход {m.get('net_return', 0) * 100:+.1f}% "
            f"(buy&hold {m.get('buy_hold', 0) * 100:+.1f}%) | "
            f"просадка {m.get('max_dd', 0) * 100:.1f}% | Sharpe {m.get('sharpe', 0):.2f} | "
            f"винрейт {m.get('win_rate', 0) * 100:.0f}% | PF {pf:.2f}")


def cmd_backtest(trader: DexTrader, args) -> int:
    rc = 1
    for key in trader.cfg.symbols:
        row = trader.store.active_strategy(key)
        if not row:
            print(f"{trader.market.name(key)}: активной стратегии нет")
            continue
        cfg = trader.pair_config(key)
        candles = trader.market.fetch_ohlcv(key, cfg.timeframe, cfg.history)
        res = run_backtest(build_features(candles), Genome.from_json(row["genome"]), cfg)
        print(f"\n=== {trader.market.name(key)} · стратегия #{row['id']} · "
              f"{len(candles)} свечей · издержки {cfg.slippage_bps:.0f} bps ===")
        print(res.summary())
        if args.trades:
            for t in res.trades:
                print(f"  {t.entry_price:>12.8g} → {t.exit_price:<12.8g} {t.pnl:+10.2f} "
                      f"({t.pnl_pct:+6.2f}%) {t.reason}")
        rc = 0
    return rc


def cmd_pine(trader: DexTrader, args) -> int:
    from ..pine import to_pine

    out_dir = Path(args.out) if args.out else Path(trader.cfg.db_path).parent / "pine"
    out_dir.mkdir(parents=True, exist_ok=True)
    rc = 1
    for key in trader.cfg.symbols:
        row = trader.store.active_strategy(key)
        if not row:
            continue
        pair = trader.market.pair(key)
        name = pair.name if pair else key
        cfg = trader.pair_config(key)
        src = to_pine(Genome.from_json(row["genome"]), name, row["timeframe"],
                      strategy_id=row["id"], score=row["score"],
                      capital=cfg.start_balance, commission_pct=cfg.taker_fee * 100,
                      max_position_frac=cfg.max_position_frac)
        src = src.replace("// Инструмент:",
                          "// DEX-пул: " + key + "\n// В TradingView этого пула может не быть — "
                          "смотри логику на любом графике той же монеты.\n// Инструмент:")
        path = out_dir / f"dex_{name.replace('/', '-')}_{row['timeframe']}_{row['id']}.pine"
        path.write_text(src, encoding="utf-8")
        print(f"{name}: → {path}")
        rc = 0
    if rc:
        print("активных стратегий нет — сначала `python dexbot.py evolve`")
    return rc


def cmd_trade(trader: DexTrader, args) -> int:
    if trader.cfg.live:
        print("⚠️  РЕАЛЬНЫЕ СВОПЫ на Solana: бот будет тратить средства кошелька "
              f"{trader.cfg.quote_symbol}.")
        print("    Мемкоины на DEX теряют 100% стоимости чаще, чем что-либо на бирже.")
        if not args.yes and input("Напиши YES, если согласен: ").strip() != "YES":
            print("Отменено.")
            return 1
    print(BANNER)
    print(f"режим: {trader.broker.name} · сеть {trader.cfg.chain} · "
          f"{len(trader.cfg.symbols)} пар · {trader.cfg.timeframe}")
    try:
        trader.run(once=args.once)
    except KeyboardInterrupt:
        print("\nостановлено пользователем")
    return 0


def cmd_reset(cfg: DexConfig, store: Storage, args) -> int:
    if not args.yes and input("Сбросить бумажный счёт, позиции и сделки? (yes) ").strip() != "yes":
        print("Отменено.")
        return 1
    store.db.executescript("DELETE FROM positions; DELETE FROM trades; DELETE FROM equity;"
                           " DELETE FROM state WHERE k IN ('paper_cash','equity_peak','day',"
                           "'paused_reason','paper_start');")
    store.db.commit()
    print(f"Счёт сброшен: {cfg.start_balance:.2f} USD")
    return 0


if __name__ == "__main__":
    sys.exit(main())
