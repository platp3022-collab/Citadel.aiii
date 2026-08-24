# -*- coding: utf-8 -*-
"""
Генетический поиск стратегии.

Как бот «придумывает» стратегию:
  1. история делится на обучение (train_frac) и валидацию (остальное);
  2. на обучении гоняется генетический алгоритм: случайная популяция геномов →
     отбор турниром → скрещивание → мутации, фитнес считается по двум половинам
     обучающего окна, чтобы отсеять подгонку под один участок;
  3. лучшие финалисты проверяются на валидации (данные, которых поиск не видел);
  4. побеждает геном с лучшим валидационным скором, прошедший фильтры качества.
     Если ни один не прошёл — стратегии нет, бот по этому символу не торгует.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass

from .backtest import Result, run_backtest
from .config import Config
from .features import Features
from .genome import Genome, crossover, mutate, random_genome

log = logging.getLogger("citadel.evolve")


@dataclass
class Candidate:
    genome: Genome
    train_score: float
    valid_score: float
    train: Result
    valid: Result

    def as_meta(self) -> dict:
        return {"train_score": round(self.train_score, 4), "valid_score": round(self.valid_score, 4),
                "train": self.train.as_dict(), "valid": self.valid.as_dict()}


def score(res: Result, cfg: Config, min_trades: int) -> float:
    """
    Фитнес: смесь Sharpe и MAR (доход / просадка) со штрафами.
    Хотим не максимальный доход, а устойчивую кривую с адекватным числом сделок.
    """
    if res.n_trades < min_trades:
        return -10.0 + res.n_trades * 0.01          # мало сделок — статистика ни о чём
    if res.end_equity <= 0:
        return -10.0
    mar = res.net_return / max(res.max_dd, 0.05)
    sharpe = max(min(res.sharpe, 8.0), -8.0)
    s = 0.6 * sharpe + 0.4 * max(min(mar, 8.0), -8.0)
    if res.net_return <= 0:
        s -= 1.0
    if res.max_dd > cfg.max_drawdown_stop:          # просадка больше стоп-лимита счёта
        s -= 3.0 * (res.max_dd - cfg.max_drawdown_stop) / max(cfg.max_drawdown_stop, 0.01)
    if res.n_trades > len(res.equity) / 3:          # перепиливание — комиссии съедят
        s -= 1.0
    if res.profit_factor and res.profit_factor < 1.0:
        s -= 0.5
    return s


def _fitness(f: Features, g: Genome, cfg: Config, start: int, end: int) -> float:
    """Скор на обучении = среднее по двум половинам минус разброс между ними."""
    mid = start + (end - start) // 2
    a = run_backtest(f, g, cfg, start, mid)
    b = run_backtest(f, g, cfg, mid, end)
    whole = run_backtest(f, g, cfg, start, end)
    sa = score(a, cfg, max(3, cfg.min_trades_train // 2))
    sb = score(b, cfg, max(3, cfg.min_trades_train // 2))
    sw = score(whole, cfg, cfg.min_trades_train)
    return 0.5 * sw + 0.5 * (min(sa, sb) * 0.6 + (sa + sb) / 2 * 0.4)


def evolve(f: Features, cfg: Config, rnd: random.Random | None = None,
           on_generation=None) -> Candidate | None:
    """Ищет стратегию под конкретный инструмент. None — ничего годного не нашлось."""
    rnd = rnd or random.Random(cfg.seed or None)
    n = len(f.candles)
    if n < f.warmup + 200:
        log.warning("мало истории (%d свечей) — поиск стратегии пропущен", n)
        return None

    split = f.warmup + int((n - f.warmup) * cfg.train_frac)
    train_start, train_end = f.warmup, split
    valid_start, valid_end = split, n
    t0 = time.time()

    population = [random_genome(rnd) for _ in range(cfg.population)]
    scored: list[tuple[float, Genome]] = []
    cache: dict[str, float] = {}

    for gen in range(cfg.generations):
        scored = []
        for g in population:
            k = g.key()
            if k not in cache:
                cache[k] = _fitness(f, g, cfg, train_start, train_end)
            scored.append((cache[k], g))
        scored.sort(key=lambda x: x[0], reverse=True)
        if on_generation:
            on_generation(gen, scored[0][0], scored[0][1])
        log.debug("поколение %d/%d: лучший фитнес %.3f", gen + 1, cfg.generations, scored[0][0])

        if gen == cfg.generations - 1:
            break

        elite = [g for _, g in scored[:cfg.elite]]
        children: list[Genome] = list(elite)
        seen = {g.key() for g in elite}
        guard = 0
        while len(children) < cfg.population and guard < cfg.population * 20:
            guard += 1
            if rnd.random() < 0.15:
                child = random_genome(rnd)                       # свежая кровь
            else:
                p1 = _tournament(scored, rnd)
                p2 = _tournament(scored, rnd)
                child = crossover(p1, p2, rnd)
                child = mutate(child, rnd, rate=0.35)
            k = child.key()
            if k in seen:
                continue
            seen.add(k)
            children.append(child)
        population = children

    # ── валидация финалистов на невиданных данных ───────────────────────────
    finalists = [g for _, g in scored[:cfg.finalists]]
    best: Candidate | None = None
    for g in finalists:
        tr = run_backtest(f, g, cfg, train_start, train_end)
        va = run_backtest(f, g, cfg, valid_start, valid_end)
        vs = score(va, cfg, cfg.min_trades_valid)
        cand = Candidate(genome=g, train_score=cache.get(g.key(), 0.0), valid_score=vs,
                         train=tr, valid=va)
        if not _acceptable(cand, cfg):
            continue
        if best is None or cand.valid_score > best.valid_score:
            best = cand

    log.info("поиск завершён за %.1fс, проверено %d уникальных геномов, %s",
             time.time() - t0, len(cache),
             "стратегия найдена" if best else "годной стратегии нет")
    if best:
        best.genome.meta = best.as_meta()
    return best


def _acceptable(c: Candidate, cfg: Config) -> bool:
    """Фильтры качества: стратегия должна работать и вне обучающей выборки."""
    if c.valid.n_trades < cfg.min_trades_valid:
        return False
    if c.valid.net_return <= 0:
        return False
    if c.valid.max_dd > cfg.max_drawdown_stop:
        return False
    if c.train.net_return <= 0:
        return False
    if c.valid.profit_factor < 1.0:
        return False
    return True


def _tournament(scored: list[tuple[float, Genome]], rnd: random.Random, k: int = 4) -> Genome:
    picks = [scored[rnd.randrange(len(scored))] for _ in range(k)]
    picks.sort(key=lambda x: x[0], reverse=True)
    return picks[0][1]
