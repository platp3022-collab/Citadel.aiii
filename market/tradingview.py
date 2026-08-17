"""Встраивание официальных виджетов TradingView.

Это законный бесплатный способ получить их данные и их же графики: TradingView
публикует виджеты для встраивания на любой сайт. Никакого скрейпинга их
закрытого API — он не документирован, нестабилен и запрещён правилами.

Что это даёт:

* живые котировки текущего года, а не то, до чего дотягивается генератор;
* график с их инструментами, таймфреймами и рисованием;
* индексные CFD (GER40, SP500, NAS100) и форекс — то, чего нет у бесплатных
  источников, которыми питается собственная панель котировок.

Виджеты грузят скрипт с s3.tradingview.com, поэтому работают только в
локально открытом файле с интернетом. Если скрипт не поднялся, на месте
виджета появляется honest-заглушка, а не пустая дыра.

Собственные графики никуда не деваются: они рисуют уровни плана, которых
TradingView знать не может, и работают без сети.
"""

from __future__ import annotations

import json
from typing import Any

TV_SCRIPT = "https://s3.tradingview.com/external-embedding/embed-widget-{}.js"

# Готовые соответствия для символов из терминала: у брокера свои имена,
# у TradingView свои.
SYMBOL_MAP = {
    "SP500": "OANDA:SPX500USD",
    "SPX500": "OANDA:SPX500USD",
    "NAS100": "OANDA:NAS100USD",
    "NAS100USD": "OANDA:NAS100USD",
    "GER40": "OANDA:DE30EUR",
    "DAX": "OANDA:DE30EUR",
    "US30": "OANDA:US30USD",
    "XAUUSD": "OANDA:XAUUSD",
    "XAGUSD": "OANDA:XAGUSD",
}

_FX = {"EUR", "GBP", "AUD", "NZD", "USD", "CAD", "CHF", "JPY"}


def tv_symbol(label: str, entry: dict[str, Any] | None = None) -> str:
    """Символ в нотации TradingView. Явное поле tv в конфиге всегда сильнее."""
    entry = entry or {}
    if entry.get("tv"):
        return str(entry["tv"])
    up = label.upper().strip()
    if up in SYMBOL_MAP:
        return SYMBOL_MAP[up]
    if entry.get("binance"):
        return "BINANCE:" + str(entry["binance"]).upper()
    if up.endswith(("USDT", "USDC")):
        return "BINANCE:" + up
    if len(up) == 6 and up[:3] in _FX and up[3:] in _FX:
        return "OANDA:" + up
    if up.endswith("USD") and len(up) in (6, 7):        # BTCUSD, ETHUSD и т.п.
        return "BINANCE:" + up[:-3] + "USDT"
    return up                                            # акции TradingView разрешит сам


TV_CSS = """
.tv-card {
  background: var(--surface);
  border: 1px solid var(--rule-soft);
  border-radius: 4px;
  box-shadow: var(--shadow);
  overflow: hidden;
}
.tv-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px 14px;
  padding: 15px 18px 13px;
  border-bottom: 1px solid var(--rule-soft);
}
.tv-head .t {
  font-family: Archivo, sans-serif;
  font-size: 16px;
  font-weight: 700;
  letter-spacing: -.012em;
}
.tv-head .sub { font-size: 13px; color: var(--ink-soft); }
.tv-holder { position: relative; }
.tv-tape { min-height: 78px; }
.tv-chart { height: 560px; }
@media (max-width: 640px) { .tv-chart { height: 420px; } }

.tv-fallback {
  display: none;
  padding: 18px;
  font-size: 13.5px;
  line-height: 1.6;
  color: var(--ink-soft);
  background: var(--surface-sunk);
}
.tv-holder.failed .tv-fallback { display: block; }
.tv-holder.failed .tradingview-widget-container { display: none; }
.tv-credit {
  padding: 8px 18px 12px;
  font-family: "IBM Plex Mono", monospace;
  font-size: 10.5px;
  color: var(--ink-faint);
}
.tv-credit a { color: var(--ink-faint); }
"""


TV_JS = r"""
(function () {
  var holders = document.querySelectorAll('.tv-holder[data-tv]');
  if (!holders.length) return;

  function currentTheme() {
    var stamped = document.documentElement.getAttribute('data-theme');
    if (stamped) return stamped;
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  function build(holder) {
    var spec;
    try { spec = JSON.parse(holder.getAttribute('data-tv')); } catch (e) { return; }
    var cfg = spec.config || {};
    var theme = currentTheme();
    cfg[spec.themeKey || 'colorTheme'] = theme;
    if (spec.kind === 'advanced-chart') cfg.theme = theme;

    holder.classList.remove('failed');
    var container = holder.querySelector('.tradingview-widget-container');
    container.innerHTML = '<div class="tradingview-widget-container__widget"></div>';

    var s = document.createElement('script');
    s.type = 'text/javascript';
    s.async = true;
    s.src = spec.src;
    s.text = JSON.stringify(cfg);
    s.onerror = function () { holder.classList.add('failed'); };
    container.appendChild(s);

    // Скрипт может «загрузиться» и ничего не отрисовать — проверяем результат.
    setTimeout(function () {
      if (!container.querySelector('iframe')) holder.classList.add('failed');
    }, 6000);
  }

  holders.forEach(build);

  // Тема страницы переключается — виджет пересобираем, сам он не умеет.
  var btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.addEventListener('click', function () {
      setTimeout(function () { holders.forEach(build); }, 60);
    });
  }
})();
"""


def _holder(kind: str, config: dict[str, Any], theme_key: str = "colorTheme",
            extra_class: str = "", fallback: str = "") -> str:
    spec = {"kind": kind, "src": TV_SCRIPT.format(kind), "config": config,
            "themeKey": theme_key}
    data = json.dumps(spec, separators=(",", ":"), ensure_ascii=False).replace('"', "&quot;")
    return (f'<div class="tv-holder {extra_class}" data-tv="{data}">'
            '<div class="tradingview-widget-container">'
            '<div class="tradingview-widget-container__widget"></div></div>'
            f'<div class="tv-fallback">{fallback}</div></div>')


def ticker_tape(symbols: list[tuple[str, str]], esc) -> str:
    """Бегущая строка котировок TradingView вместо собственной панели."""
    items = [{"proName": sym, "title": title} for title, sym in symbols]
    config = {
        "symbols": items,
        "showSymbolLogo": True,
        "isTransparent": True,
        "displayMode": "adaptive",
        "locale": "ru",
    }
    fallback = esc(
        "Виджет TradingView не загрузился. Так и должно быть, если страница открыта "
        "без интернета или опубликована в сети — их скрипт грузится с внешнего домена. "
        "Открой файл локально в браузере, и котировки пойдут.")
    return (
        '<section><div class="eyebrow"><span class="num">00</span>Live Quotes</div>'
        '<h2>Живые котировки</h2>'
        '<div class="tv-card"><div class="tv-head"><span class="t">TradingView</span>'
        '<span class="sub">котировки и цены — с их серверов, обновляются сами</span>'
        '</div>'
        + _holder("ticker-tape", config, extra_class="tv-tape", fallback=fallback)
        + '<div class="tv-credit">Данные предоставлены '
          '<a href="https://www.tradingview.com/" rel="noopener" target="_blank">TradingView</a>'
          '</div></div></section>')


def advanced_chart(symbol: str, title: str, esc, interval: str = "D") -> str:
    """Полноценный график TradingView — живой, с их инструментами."""
    config = {
        "symbol": symbol,
        "interval": interval,
        "timezone": "Etc/UTC",
        "style": "1",
        "locale": "ru",
        "autosize": True,
        "allow_symbol_change": True,
        "hide_side_toolbar": False,
        "withdateranges": True,
        "details": True,
        "studies": ["STD;EMA", "STD;SMA"],
        "support_host": "https://www.tradingview.com",
    }
    fallback = esc(
        "График TradingView не загрузился — их скрипт грузится с внешнего домена, "
        "а он доступен только в локально открытом файле с интернетом. "
        "Свечной график ниже рисуется на месте и работает всегда.")
    return (
        '<div class="tv-card"><div class="tv-head">'
        f'<span class="t">{esc(title)}</span>'
        '<span class="sub">живой график TradingView · сменить бумагу можно прямо в нём</span>'
        '</div>'
        + _holder("advanced-chart", config, theme_key="theme",
                  extra_class="tv-chart", fallback=fallback)
        + '<div class="tv-credit">График и данные — '
          '<a href="https://www.tradingview.com/" rel="noopener" target="_blank">TradingView</a>'
          '</div></div>')
