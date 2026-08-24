# -*- coding: utf-8 -*-
"""
Торговый цикл для DEX.

Отличия от биржевой версии:
  • список пар не задаётся руками, а подбирается: DexScreener → фильтры
    безопасности → проверка, что у пула есть история свечей;
  • объём сделки ограничен долей ликвидности пула, иначе бот сам себе двигает цену;
  • издержки при поиске стратегии считаются по конкретному пулу (комиссия свопа
    плюс ожидаемое влияние на цену);
  • отдельный сторож: если ликвидность пула резко просела — выходим немедленно,
    не дожидаясь сигналов стратегии.
"""
from __future__ import annotations

import copy
import logging
import math
import time

from ..bot import SymbolState, Trader
from ..evolve import evolve
from ..features import build_features
from ..genome import Genome
from ..notify import Notifier
from ..storage import Storage
from .broker import effective_slippage_bps
from .config import DexConfig
from .dexscreener import DexScreener, Pair
from .http import ApiError
from .market import DexMarket
from .safety import RugCheck, check_pair, screen

log = logging.getLogger("citadel.dex.bot")


class DexTrader(Trader):
    def __init__(self, cfg: DexConfig, store: Storage, market: DexMarket, broker,
                 notifier: Notifier, offline: bool = False):
        saved = store.get("universe", []) or []
        if saved and not cfg.symbols:
            cfg.symbols = tuple(saved)
        super().__init__(cfg, store, market, broker, notifier, offline)
        self.cfg: DexConfig = cfg
        self.market: DexMarket = market
        self.screener: DexScreener = market.screener
        self.rugcheck = RugCheck()
        self.discovered_at = float(store.get("discovered_at", 0.0) or 0.0)
        self.dashboard_mode = "dex"

    def label(self, symbol: str) -> str:
        return self.market.name(symbol)

    def trade_note(self, symbol: str, fill=None) -> str:
        """Ссылки, по которым сделку видно глазами: график пары и сама транзакция."""
        lines = []
        pair = self.market.pair(symbol)
        if pair and pair.url:
            lines.append(f'📈 <a href="{pair.url}">график на DexScreener</a>')
        if fill is not None and getattr(fill, "order_id", ""):
            lines.append(f'🔗 <a href="https://solscan.io/tx/{fill.order_id}">'
                         f'транзакция в Solscan</a>')
        return ("\n" + "\n".join(lines)) if lines else ""

    # ════════════════════════════════════════════════════════════════════════
    #  Подбор пар
    # ════════════════════════════════════════════════════════════════════════
    def candidates(self) -> list[Pair]:
        """Сырые кандидаты из DexScreener: поиск по запросам + продвигаемые токены."""
        seen: dict[str, Pair] = {}
        for query in self.cfg.discover_queries:
            try:
                found = self.screener.search(query)
            except ApiError as e:
                log.warning("поиск '%s' не удался: %s", query, e)
                continue
            for p in found:
                if p.chain == self.cfg.chain:
                    seen.setdefault(p.key, p)
        try:
            for p in self.screener.trending(self.cfg.chain):
                seen.setdefault(p.key, p)
        except ApiError as e:
            log.debug("продвигаемые токены недоступны: %s", e)
        return list(seen.values())

    @staticmethod
    def rank(p: Pair) -> float:
        """Чем крупнее и живее пул, тем выше. Крайности по обороту штрафуются."""
        liq = math.log10(max(p.liquidity_usd, 1.0))
        vol = math.log10(max(p.volume_h24, 1.0))
        score = 0.5 * liq + 0.5 * vol
        if p.liquidity_usd > 0:
            v2l = p.volume_h24 / p.liquidity_usd
            if not 0.3 <= v2l <= 5.0:                    # здоровый оборот к ликвидности
                score -= 0.5
        if p.socials or p.websites:
            score += 0.2
        return score

    def discover(self, verbose: bool = False) -> list[str]:
        """Полный цикл подбора: кандидаты → фильтры → история свечей → вселенная."""
        if self.offline:
            log.info("офлайн — оставляю сохранённый список пар")
            return list(self.cfg.symbols)
        raw = self.candidates()
        log.info("кандидатов от DexScreener: %d", len(raw))
        limits = self.cfg.safety()
        checked = screen(raw, limits, self.rugcheck, verbose=verbose)
        passed = sorted((p for p, bad in checked if not bad), key=self.rank, reverse=True)
        log.info("прошли фильтры безопасности: %d", len(passed))

        chosen: list[str] = []
        for p in passed:
            if len(chosen) >= self.cfg.universe_size:
                break
            self.market.remember(p)
            try:
                candles = self.market.fetch_ohlcv(p.key, self.cfg.timeframe, self.cfg.history)
            except (ApiError, SystemExit) as e:
                log.info("%s: свечей нет (%s)", p.name, e)
                continue
            need = 420                                   # прогрев индикаторов + обучение
            if len(candles) < need:
                log.info("%s: истории мало (%d свечей < %d)", p.name, len(candles), need)
                continue
            chosen.append(p.key)
            log.info("взял в работу: %s", p.describe())

        held = [row["symbol"] for row in self.store.all_positions()]
        universe = list(dict.fromkeys(chosen + held))     # пары с позициями не бросаем
        self.set_universe(universe)
        self.discovered_at = time.time()
        self.store.set("discovered_at", self.discovered_at)
        if chosen:
            self.notifier.send("🔎 <b>Список пар обновлён</b>\n" + "\n".join(
                f"• {self.market.name(k)}" for k in chosen))
        else:
            self.notifier.send("🔎 Ни одна пара не прошла фильтры — жду следующего круга")
        return universe

    def report(self) -> str:
        text = super().report()
        links = [f"• {self.label(k)}: {p.url}"
                 for k in self.cfg.symbols if (p := self.market.pair(k)) and p.url]
        return text + ("\n\n<b>Графики:</b>\n" + "\n".join(links) if links else "")

    def set_universe(self, symbols: list[str]) -> None:
        self.cfg.symbols = tuple(symbols)
        for symbol in symbols:
            self.state.setdefault(symbol, SymbolState())
        for symbol in list(self.state):
            if symbol not in symbols:
                del self.state[symbol]
        self.store.set("universe", symbols)
        self._load_strategies()

    def maybe_retrain(self) -> None:
        age_h = (time.time() - self.discovered_at) / 3600.0
        if not self.cfg.symbols or age_h >= self.cfg.rediscover_hours:
            self.discover()
        super().maybe_retrain()

    # ════════════════════════════════════════════════════════════════════════
    #  Поиск стратегии с издержками конкретного пула
    # ════════════════════════════════════════════════════════════════════════
    def pair_config(self, symbol: str) -> DexConfig:
        """Копия конфига, где проскальзывание учитывает влияние на цену в этом пуле."""
        cfg = copy.copy(self.cfg)
        liquidity = self.market.liquidity(symbol)
        if liquidity > 0:
            cfg.slippage_bps = effective_slippage_bps(self.cfg, liquidity,
                                                      self.broker.equity({}) or None)
        return cfg

    def train(self, symbol: str, force: bool = False) -> Genome | None:
        st = self.state[symbol]
        cfg = self.pair_config(symbol)
        log.info("%s: ищу стратегию (проскальзывание для пула %.0f bps)…",
                 self.label(symbol), cfg.slippage_bps)
        try:
            candles = self._candles(symbol)
        except (ApiError, SystemExit) as e:
            log.warning("%s: свечей нет (%s)", symbol, e)
            return st.genome
        feats = build_features(candles)
        cand = evolve(feats, cfg)
        st.trained_at = time.time()
        if cand is None:
            self.notifier.send(f"🔍 {self.label(symbol)}: годной стратегии нет — "
                               f"по этой паре не торгую")
            return st.genome if not force else None
        better = st.genome is None or cand.valid_score > st.valid_score * (1 + cfg.adopt_margin)
        sid = self.store.save_strategy(symbol, cfg.timeframe, cand.genome,
                                       cand.valid_score, cand.as_meta())
        if better:
            self.store.activate(sid, symbol)
            st.genome, st.strategy_id, st.valid_score = cand.genome, sid, cand.valid_score
            self.notifier.send(
                f"🧠 <b>{self.market.name(symbol)}: новая стратегия #{sid}</b> "
                f"(скор {cand.valid_score:.2f})\n<pre>{cand.genome.describe()}</pre>\n"
                f"обучение: {cand.train.summary()}\nвалидация: {cand.valid.summary()}")
        return st.genome

    # ════════════════════════════════════════════════════════════════════════
    #  Ограничения и сторожа DEX
    # ════════════════════════════════════════════════════════════════════════
    def extra_size_cap(self, symbol: str, price: float) -> float | None:
        """Не больше доли ликвидности пула — иначе своп сам себя и сдвинет."""
        liquidity = self.market.liquidity(symbol)
        if liquidity <= 0 or price <= 0:
            return None
        return liquidity * self.cfg.max_pool_frac / price

    def emergency_exit_reason(self, symbol: str) -> str | None:
        """Слив ликвидности — выходим сразу, стратегия тут уже не поможет."""
        if self.offline:
            return None
        row = self.store.get_position(symbol)
        if not row or row["qty"] <= 0:
            return None
        before = float(self.store.get(f"liq:{symbol}", 0.0) or 0.0)
        pair = self.market.refresh_pair(symbol)
        if pair is None:
            return None
        if pair.liquidity_usd < self.cfg.min_liquidity_usd * 0.5:
            return "ликвидность ниже половины минимума"
        if before > 0 and pair.liquidity_usd < before * (1 - self.cfg.rug_liquidity_drop):
            return (f"ликвидность упала с ${before:,.0f} до ${pair.liquidity_usd:,.0f}")
        return None

    def _open(self, symbol: str, st: SymbolState) -> None:
        """Перед входом ещё раз сверяемся с фильтрами — пара могла испортиться."""
        if not self.offline:
            pair = self.market.refresh_pair(symbol)
            if pair is None:
                return
            bad = check_pair(pair, self.cfg.safety())
            if bad:
                log.info("%s: вход отменён — %s", self.market.name(symbol), "; ".join(bad))
                return
            self.store.set(f"liq:{symbol}", pair.liquidity_usd)
        super()._open(symbol, st)
