# -*- coding: utf-8 -*-
"""
Фильтры «можно ли это вообще торговать».

На DEX листинг ничего не значит: пул создаётся за минуту, ликвидность
вынимается за секунду. Поэтому до всякого поиска стратегии пара проходит
проверки — ликвидность, возраст, объём, баланс покупок и продаж, а для
Solana ещё и RugCheck (право доминтить, заморозка, блокировка LP).

Проверки возвращают список причин отказа: пусто — пару можно торговать.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .dexscreener import Pair
from .http import ApiError, HttpClient

log = logging.getLogger("citadel.dex.safety")

RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"


@dataclass
class SafetyLimits:
    min_liquidity_usd: float = 50_000.0     # мельче — выходить из позиции будет некуда
    min_volume_h24_usd: float = 100_000.0   # без оборота нет и цены
    min_age_hours: float = 72.0             # свежие пулы — территория рагов
    max_age_hours: float = 0.0              # 0 = без ограничения сверху
    min_txns_h24: int = 200
    max_buy_sell_skew: float = 5.0          # покупок в N раз больше продаж — похоже на ботов
    min_volume_to_liquidity: float = 0.15   # оборот меньше 15% ликвидности — пул стоячий
    max_volume_to_liquidity: float = 30.0   # оборот ≫ ликвидности — похоже на wash-трейдинг
    max_fdv_to_liquidity: float = 400.0     # вся капитализация на тонком пуле
    require_socials: bool = False
    rugcheck: bool = True                   # для Solana дополнительно спросить RugCheck
    max_rugcheck_score: float = 1500.0      # шкала RugCheck: больше — хуже


def check_pair(p: Pair, limits: SafetyLimits) -> list[str]:
    """Причины, по которым пару торговать не стоит. Пустой список — годится."""
    bad: list[str] = []
    if p.liquidity_usd < limits.min_liquidity_usd:
        bad.append(f"ликвидность ${p.liquidity_usd:,.0f} < ${limits.min_liquidity_usd:,.0f}")
    if p.volume_h24 < limits.min_volume_h24_usd:
        bad.append(f"объём за сутки ${p.volume_h24:,.0f} < ${limits.min_volume_h24_usd:,.0f}")
    if p.created_at_ms and p.age_hours < limits.min_age_hours:
        bad.append(f"пулу {p.age_hours:.1f} ч — младше {limits.min_age_hours:.0f} ч")
    if limits.max_age_hours and p.age_hours > limits.max_age_hours:
        bad.append(f"пулу {p.age_hours / 24:.0f} д — старше лимита")
    txns = p.txns_h24_buys + p.txns_h24_sells
    if txns < limits.min_txns_h24:
        bad.append(f"сделок за сутки {txns} < {limits.min_txns_h24}")
    if p.txns_h24_sells > 0:
        skew = p.txns_h24_buys / p.txns_h24_sells
        if skew > limits.max_buy_sell_skew:
            bad.append(f"покупок в {skew:.1f}× больше продаж — почти никто не выходит")
    elif p.txns_h24_buys > 50:
        bad.append("продаж нет вообще — из токена, похоже, не выпускают")
    if p.liquidity_usd > 0:
        v2l = p.volume_h24 / p.liquidity_usd
        if v2l < limits.min_volume_to_liquidity:
            bad.append(f"оборот всего {v2l * 100:.0f}% ликвидности — пул стоячий")
        if v2l > limits.max_volume_to_liquidity:
            bad.append(f"оборот в {v2l:.0f}× ликвидности — похоже на wash-трейдинг")
        if p.fdv and p.fdv / p.liquidity_usd > limits.max_fdv_to_liquidity:
            bad.append(f"FDV в {p.fdv / p.liquidity_usd:.0f}× ликвидности — тонкий пул")
    if limits.require_socials and not (p.socials or p.websites):
        bad.append("нет ни сайта, ни соцсетей")
    return bad


class RugCheck:
    """Проверка Solana-токена через RugCheck: авторитеты минта, блокировка LP, риски."""

    def __init__(self, client: HttpClient | None = None):
        self.http = client or HttpClient(min_interval=0.5)

    def check(self, mint: str, limits: SafetyLimits) -> list[str]:
        try:
            data = self.http.get_json(RUGCHECK.format(mint=mint))
        except ApiError as e:
            log.warning("RugCheck недоступен для %s: %s", mint, e)
            return []                                  # нет ответа — не выдумываем вердикт
        bad: list[str] = []
        score = data.get("score_normalised", data.get("score"))
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = None
        if score is not None and score > limits.max_rugcheck_score:
            bad.append(f"RugCheck score {score:.0f} > {limits.max_rugcheck_score:.0f}")
        for risk in data.get("risks") or []:
            name = str(risk.get("name", "")).lower()
            level = str(risk.get("level", "")).lower()
            if level in ("danger", "high") or "authority" in name or "lp" in name:
                bad.append(f"RugCheck: {risk.get('name')} ({risk.get('level')})")
        return bad


def screen(pairs: list[Pair], limits: SafetyLimits, rugcheck: RugCheck | None = None,
           verbose: bool = False) -> list[tuple[Pair, list[str]]]:
    """Прогоняет список пар через фильтры. Возвращает (пара, причины отказа)."""
    out: list[tuple[Pair, list[str]]] = []
    for p in pairs:
        bad = check_pair(p, limits)
        if not bad and limits.rugcheck and p.chain == "solana" and rugcheck is not None:
            bad += rugcheck.check(p.base_address, limits)
        if verbose:
            log.info("%s: %s", p.name, "ок" if not bad else "; ".join(bad))
        out.append((p, bad))
    return out
