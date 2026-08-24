# -*- coding: utf-8 -*-
"""
Торговый цикл.

Что делает бот на каждом круге:
  1. следит за открытыми позициями по текущей цене — стоп, тейк, трейлинг;
  2. на закрытии очередной свечи пересчитывает индикаторы и проверяет
     сигналы входа/выхода активной стратегии символа;
  3. раз в `retrain_hours` заново ищет стратегию и меняет активную, если новая
     заметно лучше на валидации;
  4. держит защиты: лимит одновременных позиций, дневной убыток, общая просадка.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .backtest import Result
from .broker import Fill
from .config import TIMEFRAME_SECONDS, Config
from .evolve import evolve
from .features import Features, build_features
from .genome import Genome
from .market import Market
from .notify import Notifier
from .storage import Storage

log = logging.getLogger("citadel.bot")


@dataclass
class SymbolState:
    genome: Genome | None = None
    strategy_id: int | None = None
    features: Features | None = None
    last_bar_ts: int = 0
    trained_at: float = 0.0
    valid_score: float = 0.0


class Trader:
    def __init__(self, cfg: Config, store: Storage, market: Market, broker,
                 notifier: Notifier, offline: bool = False):
        self.cfg, self.store, self.market = cfg, store, market
        self.broker, self.notifier, self.offline = broker, notifier, offline
        self.state: dict[str, SymbolState] = {s: SymbolState() for s in cfg.symbols}
        self.bar_seconds = TIMEFRAME_SECONDS.get(cfg.timeframe, 3600)
        self._load_strategies()

    # ════════════════════════════════════════════════════════════════════════
    #  Стратегии
    # ════════════════════════════════════════════════════════════════════════
    def _load_strategies(self) -> None:
        for symbol, st in self.state.items():
            row = self.store.active_strategy(symbol)
            if row:
                st.genome = Genome.from_json(row["genome"])
                st.strategy_id = row["id"]
                st.trained_at = float(row["created_at"])
                st.valid_score = float(row["score"] or 0.0)
                log.info("%s: загружена стратегия #%d (скор %.2f)", symbol, row["id"], st.valid_score)

    def _candles(self, symbol: str, limit: int | None = None):
        return self.market.fetch_ohlcv(symbol, self.cfg.timeframe,
                                       limit or self.cfg.history, offline=self.offline)

    def train(self, symbol: str, force: bool = False) -> Genome | None:
        """Ищет стратегию по свежей истории и активирует её, если она лучше текущей."""
        st = self.state[symbol]
        log.info("%s: ищу стратегию…", symbol)
        candles = self._candles(symbol)
        feats = build_features(candles)
        rnd = random.Random(self.cfg.seed or None)
        cand = evolve(feats, self.cfg, rnd)
        st.trained_at = time.time()
        if cand is None:
            self.notifier.send(f"🔍 {symbol}: годной стратегии не нашлось — по этому символу не торгую")
            if st.genome and not force:
                return st.genome
            return None

        better = st.genome is None or cand.valid_score > st.valid_score * (1 + self.cfg.adopt_margin) \
            or (st.valid_score <= 0 and cand.valid_score > 0)
        sid = self.store.save_strategy(symbol, self.cfg.timeframe, cand.genome,
                                       cand.valid_score, cand.as_meta())
        if better:
            self.store.activate(sid, symbol)
            st.genome, st.strategy_id, st.valid_score = cand.genome, sid, cand.valid_score
            self.notifier.send(
                f"🧠 <b>{symbol}: новая стратегия #{sid}</b> (скор {cand.valid_score:.2f})\n"
                f"<pre>{cand.genome.describe()}</pre>\n"
                f"обучение: {cand.train.summary()}\nвалидация: {cand.valid.summary()}")
        else:
            log.info("%s: найденная стратегия (%.2f) не лучше текущей (%.2f) — оставляю прежнюю",
                     symbol, cand.valid_score, st.valid_score)
        return st.genome

    def maybe_retrain(self) -> None:
        for symbol, st in self.state.items():
            age_h = (time.time() - st.trained_at) / 3600.0
            if st.genome is None or age_h >= self.cfg.retrain_hours:
                self.train(symbol)

    # ════════════════════════════════════════════════════════════════════════
    #  Защиты счёта
    # ════════════════════════════════════════════════════════════════════════
    def trading_allowed(self, equity: float) -> tuple[bool, str]:
        peak = float(self.store.get("equity_peak", equity) or equity)
        if equity > peak:
            peak = equity
            self.store.set("equity_peak", peak)
        dd = 1.0 - equity / peak if peak > 0 else 0.0
        if dd >= self.cfg.max_drawdown_stop:
            return False, f"просадка счёта {dd*100:.1f}% ≥ лимита {self.cfg.max_drawdown_stop*100:.0f}%"

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day = self.store.get("day", {}) or {}
        if day.get("date") != today:
            day = {"date": today, "start_equity": equity}
            self.store.set("day", day)
        start_eq = float(day.get("start_equity") or equity)
        if start_eq > 0:
            loss = 1.0 - equity / start_eq
            if loss >= self.cfg.daily_loss_stop:
                return False, f"дневной убыток {loss*100:.1f}% ≥ лимита {self.cfg.daily_loss_stop*100:.0f}%"
        return True, ""

    # ════════════════════════════════════════════════════════════════════════
    #  Основной цикл
    # ════════════════════════════════════════════════════════════════════════
    def prices(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for symbol, st in self.state.items():
            try:
                out[symbol] = self.market.last_price(symbol) if not self.offline else (
                    st.features.candles.close[-1] if st.features else 0.0)
            except Exception as e:                       # noqa: BLE001 — сеть, продолжаем по остальным
                log.warning("%s: не удалось получить цену: %s", symbol, e)
                if st.features:
                    out[symbol] = st.features.candles.close[-1]
        return out

    def tick(self) -> None:
        fresh = self._refresh_bars()                 # 1. подтянуть закрывшиеся свечи
        prices = self.prices()                       # 2. текущие цены
        equity = self.broker.equity(prices)
        self.store.log_equity(equity, self.broker.cash)
        allowed, reason = self.trading_allowed(equity)

        for row in self.store.all_positions():       # 3. стоп/тейк/трейлинг
            px = prices.get(row["symbol"])
            if px:
                self._manage_position(row, px)

        for symbol in fresh:                         # 4. сигналы входа и выхода
            self._on_bar_close(symbol, self.state[symbol], allowed)

        if not allowed:
            self._pause_once(reason)

    def _refresh_bars(self) -> list[str]:
        """Обновляет свечи и индикаторы по символам, где закрылся новый бар."""
        fresh: list[str] = []
        for symbol, st in self.state.items():
            if not self._new_bar_due(st):
                continue
            try:
                candles = self._candles(symbol, limit=max(600, self.cfg.history // 3))
            except Exception as e:                   # noqa: BLE001 — сеть, идём дальше
                log.warning("%s: свечи не пришли: %s", symbol, e)
                continue
            if not len(candles) or candles.ts[-1] == st.last_bar_ts:
                continue
            st.features = build_features(candles)
            st.last_bar_ts = candles.ts[-1]
            fresh.append(symbol)
        return fresh

    def _new_bar_due(self, st: SymbolState) -> bool:
        if st.features is None or st.last_bar_ts == 0:
            return True
        return time.time() >= (st.last_bar_ts / 1000.0) + 2 * self.bar_seconds

    def _on_bar_close(self, symbol: str, st: SymbolState, allowed: bool) -> None:
        if st.genome is None or st.features is None:
            return
        f, g = st.features, st.genome
        i = len(f.candles) - 1
        pos = self.store.get_position(symbol)

        if pos and pos["qty"] > 0:
            bars = int(pos["bars"] or 0) + 1
            self.store.upsert_position(symbol, **{**_row_to_kw(pos), "bars": bars})
            hit_exit = any(f.signals[k][i] for k in g.exit if k in f.signals)
            if hit_exit or bars >= g.max_hold:
                self._close(symbol, f.candles.close[-1], "signal" if hit_exit else "max_hold")
            return

        if not allowed:
            return
        if len(self.store.all_positions()) >= self.cfg.max_positions:
            return
        entry_ok = bool(g.entry) and all(f.signals[k][i] for k in g.entry if k in f.signals)
        if entry_ok:
            self._open(symbol, st)

    # ── сделки ──────────────────────────────────────────────────────────────
    def _open(self, symbol: str, st: SymbolState) -> None:
        f, g = st.features, st.genome
        atr = f.series["atr14"][-1]
        if atr != atr or atr <= 0:
            return
        try:
            price = self.market.last_price(symbol) if not self.offline else f.candles.close[-1]
        except Exception as e:                            # noqa: BLE001
            log.warning("%s: нет цены для входа: %s", symbol, e)
            return
        stop = price - g.stop_atr * atr
        risk_per_unit = price - stop
        if risk_per_unit <= 0:
            return
        equity = self.broker.equity({symbol: price})
        cash = self.broker.cash
        qty = min(equity * (g.risk_pct / 100.0) / risk_per_unit,
                  equity * self.cfg.max_position_frac / price,
                  cash / (price * (1 + self.cfg.taker_fee)) * 0.995)
        qty = self.market.amount_to_precision(symbol, qty) if self.market.ex else qty
        min_cost = self.market.min_notional(symbol) if self.market.ex else self.cfg.min_notional
        if qty <= 0 or qty * price < min_cost:
            log.info("%s: сигнал есть, но объём %.8f ниже минимума — пропускаю", symbol, qty)
            return

        fill: Fill = self.broker.buy(symbol, qty, price)
        if fill.qty <= 0:
            return
        take = fill.price + g.take_atr * atr if g.take_atr else 0.0
        self.store.upsert_position(
            symbol, qty=fill.qty, entry_price=fill.price, entry_fee=fill.fee,
            stop=fill.price - g.stop_atr * atr,
            take=take, trail=0.0, peak=fill.price, opened_at=int(time.time()),
            opened_bar=int(f.candles.ts[-1]), bars=0, strategy_id=st.strategy_id)
        self.store.log_trade(symbol, "buy", fill.qty, fill.price, fill.cost, fill.fee, 0.0,
                             "entry", self.broker.live, fill.order_id)
        self.notifier.send(
            f"🟢 <b>Покупка {symbol}</b>\nцена {fill.price:.6g}, объём {fill.qty:.8g} "
            f"(≈{fill.cost:.2f} {self.cfg.quote})\nстоп {fill.price - g.stop_atr * atr:.6g}"
            + (f", тейк {take:.6g}" if take else "")
            + f"\nстратегия #{st.strategy_id} · {self.broker.name}")

    def _manage_position(self, row, price: float) -> None:
        symbol = row["symbol"]
        st = self.state.get(symbol)
        g = st.genome if st else None
        stop = float(row["stop"] or 0.0)
        trail = float(row["trail"] or 0.0)
        take = float(row["take"] or 0.0)
        peak = max(float(row["peak"] or 0.0), price)
        eff_stop = max(stop, trail)

        if eff_stop and price <= eff_stop:
            self._close(symbol, price, "trail" if trail > stop else "stop")
            return
        if take and price >= take:
            self._close(symbol, price, "take")
            return
        if g and g.trail_atr and st and st.features:
            atr = st.features.series["atr14"][-1]
            if atr == atr and atr > 0:
                trail = max(trail, peak - g.trail_atr * atr)
        if peak != float(row["peak"] or 0.0) or trail != float(row["trail"] or 0.0):
            self.store.upsert_position(symbol, **{**_row_to_kw(row), "peak": peak, "trail": trail})

    def _close(self, symbol: str, price: float, reason: str) -> None:
        row = self.store.get_position(symbol)
        if not row or row["qty"] <= 0:
            return
        qty = float(row["qty"])
        try:
            fill: Fill = self.broker.sell(symbol, qty, price)
        except Exception as e:                            # noqa: BLE001 — биржа могла отклонить
            log.error("%s: не удалось продать: %s", symbol, e)
            self.notifier.send(f"⚠️ {symbol}: ордер на продажу не прошёл — {e}")
            return
        # полная стоимость входа = оборот + комиссия покупки, иначе P&L будет завышен
        entry_cost = float(row["entry_price"]) * fill.qty + float(row["entry_fee"] or 0.0)
        pnl = fill.cost - fill.fee - entry_cost
        pnl_pct = pnl / entry_cost * 100.0 if entry_cost else 0.0
        self.store.log_trade(symbol, "sell", fill.qty, fill.price, fill.cost, fill.fee, pnl,
                             reason, self.broker.live, fill.order_id)
        self.store.drop_position(symbol)
        icon = "🔴" if pnl < 0 else "✅"
        self.notifier.send(
            f"{icon} <b>Продажа {symbol}</b> ({_REASON.get(reason, reason)})\n"
            f"вход {float(row['entry_price']):.6g} → выход {fill.price:.6g}\n"
            f"P&L {pnl:+.2f} {self.cfg.quote} ({pnl_pct:+.2f}%)")

    def _pause_once(self, reason: str) -> None:
        if self.store.get("paused_reason") != reason:
            self.store.set("paused_reason", reason)
            self.notifier.send(f"⛔️ Торговля на паузе: {reason}. Открытые позиции доводятся до выхода.")

    # ── отчёт ───────────────────────────────────────────────────────────────
    def report(self) -> str:
        prices = self.prices()
        equity = self.broker.equity(prices)
        start = float(self.store.get("paper_start", self.cfg.start_balance) or self.cfg.start_balance)
        lines = [f"📊 <b>Citadel Trader</b> · {self.broker.name}",
                 f"Эквити: {equity:.2f} {self.cfg.quote} ({(equity/start-1)*100:+.2f}% от старта)",
                 f"Свободно: {self.broker.cash:.2f} {self.cfg.quote}"]
        positions = self.store.all_positions()
        if positions:
            lines.append("\n<b>Позиции:</b>")
            for p in positions:
                px = prices.get(p["symbol"], p["entry_price"])
                chg = (px / p["entry_price"] - 1) * 100 if p["entry_price"] else 0.0
                lines.append(f"• {p['symbol']}: {p['qty']:.6g} @ {p['entry_price']:.6g} → {px:.6g} ({chg:+.2f}%)")
        else:
            lines.append("\nОткрытых позиций нет.")
        lines.append("\n<b>Стратегии:</b>")
        for symbol, st in self.state.items():
            if st.genome:
                lines.append(f"• {symbol} #{st.strategy_id} (скор {st.valid_score:.2f}): "
                             + ", ".join(st.genome.entry))
            else:
                lines.append(f"• {symbol}: стратегии нет — не торгую")
        trades = self.store.recent_trades(5)
        if trades:
            lines.append("\n<b>Последние сделки:</b>")
            for t in trades:
                when = datetime.fromtimestamp(t["ts"], timezone.utc).strftime("%d.%m %H:%M")
                pnl = f" {t['pnl']:+.2f}" if t["side"] == "sell" else ""
                lines.append(f"• {when} {t['side']} {t['symbol']} @ {t['price']:.6g}{pnl}")
        return "\n".join(lines)

    def run(self, once: bool = False) -> None:
        self.store.set("paper_start", self.store.get("paper_start", self.cfg.start_balance))
        self.maybe_retrain()
        self.notifier.send(f"🚀 Citadel Trader запущен · {self.broker.name} · "
                           f"{', '.join(self.cfg.symbols)} · {self.cfg.timeframe}")
        last_retrain_check = last_report = time.time()
        while True:
            try:
                self.tick()
            except KeyboardInterrupt:
                raise
            except Exception as e:                        # noqa: BLE001 — цикл не должен падать
                log.exception("ошибка в цикле: %s", e)
            if once:
                return
            now = time.time()
            if now - last_retrain_check > 1800:
                last_retrain_check = now
                self.maybe_retrain()
            if now - last_report > 86400:
                last_report = now
                self.notifier.send(self.report())
            time.sleep(self.cfg.poll_seconds)


_REASON = {"stop": "стоп-лосс", "take": "тейк-профит", "trail": "трейлинг-стоп",
           "signal": "сигнал выхода", "max_hold": "лимит времени в позиции", "end": "закрытие"}


def _row_to_kw(row) -> dict:
    return {k: row[k] for k in ("qty", "entry_price", "entry_fee", "stop", "take", "trail",
                                "peak", "opened_at", "opened_bar", "bars", "strategy_id")}
