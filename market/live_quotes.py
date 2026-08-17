"""Панель живых котировок: цены тикают в реальном времени.

Работает только в локально открытом файле — страница сама держит соединение
с источником котировок. В опубликованном виде (claude.ai) политика безопасности
режет любые внешние запросы, там панель честно покажет «нет соединения».

Источники:

* Binance — крипта, без ключа, поток `@miniTicker` толкает свечу раз в секунду.
  CORS открыт, работает из браузера как есть.
* Finnhub — акции и форекс, нужен бесплатный ключ (finnhub.io/register).
  Стартовая цена закрытия берётся через REST `/quote`, дальше сделки идут
  по вебсокету, а процент считается от вчерашнего закрытия.

Символы, для которых источник не задан, показываются серым с пометкой —
инструмент не выдумывает цену, которой у него нет.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Символы из терминала: крипта тикает без ключа, остальное просит ключ Finnhub.
DEFAULT_LIVE: dict[str, Any] = {
    "refresh_ms": 1000,
    "finnhub_key": "",
    "groups": [
        {
            "name": "Основные",
            "symbols": [
                {"label": "EURUSD", "finnhub": "OANDA:EUR_USD"},
                {"label": "GBPUSD", "finnhub": "OANDA:GBP_USD"},
                {"label": "XAGUSD", "finnhub": "OANDA:XAG_USD"},
                {"label": "XAUUSD", "finnhub": "OANDA:XAU_USD"},
            ],
        },
        {
            "name": "Крипта",
            "symbols": [
                {"label": "BTCUSDT", "binance": "BTCUSDT"},
                {"label": "ETHUSD", "binance": "ETHUSDT"},
                {"label": "SOLUSDT", "binance": "SOLUSDT"},
                {"label": "XRPUSD", "binance": "XRPUSDT"},
                {"label": "DOGEUSDT", "binance": "DOGEUSDT"},
                {"label": "BNBUSDT", "binance": "BNBUSDT"},
            ],
        },
        {
            "name": "Индексы",
            "symbols": [
                {"label": "GER40", "note": "CFD-символ брокера, у Finnhub нет"},
                {"label": "SP500", "finnhub": "^GSPC",
                 "note": "индекс, не CFD — цена расходится с терминалом"},
                {"label": "NAS100USD", "finnhub": "^NDX",
                 "note": "индекс, не CFD — цена расходится с терминалом"},
            ],
        },
    ],
}


def load_live_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return DEFAULT_LIVE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return DEFAULT_LIVE
    if not isinstance(payload, dict) or not payload.get("groups"):
        return DEFAULT_LIVE
    payload.setdefault("refresh_ms", 1000)
    payload.setdefault("finnhub_key", "")
    return payload


# ════════════════════════════════════════════════════════════════════════════
#  СТИЛИ
# ════════════════════════════════════════════════════════════════════════════

LIVE_CSS = """
.live-card {
  background: var(--surface);
  border: 1px solid var(--rule-soft);
  border-radius: 4px;
  box-shadow: var(--shadow);
  overflow: hidden;
}

.live-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px 14px;
  padding: 14px 18px 12px;
  border-bottom: 1px solid var(--rule-soft);
}
.live-head .t {
  font-family: Archivo, sans-serif;
  font-size: 16px;
  font-weight: 700;
  letter-spacing: -.012em;
}
.live-head .sub { font-size: 13px; color: var(--ink-soft); }

.conn {
  margin-left: auto;
  display: flex;
  flex-wrap: wrap;
  gap: 6px 10px;
  font-family: "IBM Plex Mono", monospace;
  font-size: 11px;
}
.conn span {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 8px;
  border-radius: 2px;
  background: var(--surface-sunk);
  color: var(--ink-faint);
  white-space: nowrap;
}
.conn span::before {
  content: "";
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--ink-faint);
}
.conn span.live { color: var(--long); }
.conn span.live::before { background: var(--long); }
.conn span.down { color: var(--short); }
.conn span.down::before { background: var(--short); }

/* min-width перебивает общее правило для table — иначе панель распирает страницу. */
.live-table { width: 100%; min-width: 0; border-collapse: collapse; }
.live-table th {
  font-family: "IBM Plex Mono", monospace;
  font-size: 10.5px;
  font-weight: 500;
  letter-spacing: .1em;
  text-transform: uppercase;
  color: var(--ink-faint);
  text-align: right;
  padding: 9px 16px;
  background: var(--surface-sunk);
  border-bottom: 1px solid var(--rule);
}
.live-table th:first-child { text-align: left; }

.live-table tr.grp td {
  font-family: "IBM Plex Mono", monospace;
  font-size: 10.5px;
  letter-spacing: .12em;
  text-transform: uppercase;
  color: var(--ink-faint);
  background: var(--surface-sunk);
  padding: 7px 16px;
  border-bottom: 1px solid var(--rule-soft);
}

.live-table tbody td {
  padding: 9px 16px;
  border-bottom: 1px solid var(--rule-soft);
  font-family: "IBM Plex Mono", monospace;
  font-variant-numeric: tabular-nums;
  font-size: 13.5px;
  text-align: right;
  white-space: nowrap;
}
.live-table tbody tr:last-child td { border-bottom: none; }
.live-table td.sym {
  text-align: left;
  font-family: Archivo, sans-serif;
  font-weight: 700;
  font-size: 13.5px;
  letter-spacing: -.01em;
}
.live-table td.sym small {
  display: block;
  font-family: "IBM Plex Sans", sans-serif;
  font-weight: 400;
  font-size: 11px;
  letter-spacing: 0;
  color: var(--ink-faint);
  margin-top: 1px;
  white-space: normal;
}
.live-table tr.idle td { color: var(--ink-faint); }

.px { position: relative; transition: background-color .45s ease-out; }
.px.up-tick { background: var(--long-soft); }
.px.down-tick { background: var(--short-soft); }
.chg.up, .pct.up { color: var(--long); }
.chg.down, .pct.down { color: var(--short); }

.live-note {
  padding: 10px 18px 12px;
  font-family: "IBM Plex Mono", monospace;
  font-size: 10.5px;
  line-height: 1.6;
  color: var(--ink-faint);
}

@media (prefers-reduced-motion: reduce) {
  .px { transition: none; }
}

@media (max-width: 560px) {
  .live-table th, .live-table tbody td { padding: 8px 10px; font-size: 12.5px; }
  .live-table th.hide-s, .live-table td.hide-s { display: none; }
}
"""


# ════════════════════════════════════════════════════════════════════════════
#  ДВИЖОК
# ════════════════════════════════════════════════════════════════════════════

LIVE_JS = r"""
(function () {
  var root = document.getElementById('live-quotes');
  if (!root) return;
  var cfg;
  try { cfg = JSON.parse(root.getAttribute('data-live')); } catch (e) { return; }

  var rows = {};                       // label -> {el, price, prev, decimals}
  document.querySelectorAll('#live-quotes tr[data-label]').forEach(function (tr) {
    rows[tr.getAttribute('data-label')] = {
      tr: tr,
      px: tr.querySelector('.px'),
      chg: tr.querySelector('.chg'),
      pct: tr.querySelector('.pct'),
      last: null,
      prevClose: null,
      decimals: parseInt(tr.getAttribute('data-decimals'), 10) || 2,
      dirty: false
    };
  });

  function setConn(id, state, text) {
    var el = document.getElementById('conn-' + id);
    if (!el) return;
    el.className = state;
    el.textContent = text;
  }

  function fmt(v, d) {
    return v.toLocaleString('ru-RU', {minimumFractionDigits: d, maximumFractionDigits: d});
  }

  // Приход цены только копит значение; рисуем раз в тик, чтобы не дёргать DOM.
  function push(label, price, prevClose) {
    var r = rows[label];
    if (!r || !isFinite(price)) return;
    if (prevClose !== undefined && prevClose !== null && isFinite(prevClose)) {
      r.prevClose = prevClose;
    }
    r.pending = price;
    r.dirty = true;
  }

  function render() {
    Object.keys(rows).forEach(function (label) {
      var r = rows[label];
      if (!r.dirty) return;
      r.dirty = false;
      var price = r.pending;
      var prior = r.last;
      r.last = price;
      r.tr.classList.remove('idle');
      r.px.textContent = fmt(price, r.decimals);

      if (prior !== null && price !== prior) {
        var cls = price > prior ? 'up-tick' : 'down-tick';
        r.px.classList.remove('up-tick', 'down-tick');
        void r.px.offsetWidth;                    // перезапуск подсветки
        r.px.classList.add(cls);
        setTimeout(function () { r.px.classList.remove(cls); }, 450);
      }

      if (r.prevClose) {
        var d = price - r.prevClose;
        var p = d / r.prevClose * 100;
        var dir = d > 0 ? 'up' : (d < 0 ? 'down' : '');
        r.chg.textContent = (d > 0 ? '+' : '') + fmt(d, r.decimals);
        r.pct.textContent = (p > 0 ? '+' : '') + p.toFixed(2) + '%';
        r.chg.className = 'chg ' + dir;
        r.pct.className = 'pct ' + dir;
      }
    });
  }
  setInterval(render, Math.max(200, cfg.refresh_ms || 1000));

  // ── Binance: крипта, без ключа, поток раз в секунду ────────────────────
  var binance = cfg.binance || {};                // streamSymbol -> label
  var bnSymbols = Object.keys(binance);
  if (bnSymbols.length) {
    var streams = bnSymbols.map(function (s) { return s.toLowerCase() + '@miniTicker'; });
    var url = 'wss://stream.binance.com:9443/stream?streams=' + streams.join('/');
    var bnRetry = 0;

    function connectBinance() {
      setConn('binance', '', 'binance: подключение');
      var ws;
      try { ws = new WebSocket(url); } catch (e) { return retryBinance(); }
      ws.onopen = function () { bnRetry = 0; setConn('binance', 'live', 'binance: онлайн'); };
      ws.onmessage = function (ev) {
        var msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        var d = msg.data || msg;
        if (!d || !d.s) return;
        var label = binance[d.s];
        if (!label) return;
        push(label, parseFloat(d.c), parseFloat(d.o));
      };
      ws.onclose = retryBinance;
      ws.onerror = function () { try { ws.close(); } catch (e) {} };
    }

    function retryBinance() {
      setConn('binance', 'down', 'binance: нет связи');
      bnRetry++;
      setTimeout(connectBinance, Math.min(30000, 1000 * Math.pow(2, bnRetry)));
    }
    connectBinance();
  }

  // ── Finnhub: акции и форекс, нужен ключ ───────────────────────────────
  var fh = cfg.finnhub || {};                     // apiSymbol -> label
  var fhSymbols = Object.keys(fh);
  if (fhSymbols.length) {
    if (!cfg.finnhub_key) {
      setConn('finnhub', 'down', 'finnhub: нет ключа');
    } else {
      // Вчерашнее закрытие — чтобы было от чего считать процент.
      fhSymbols.forEach(function (sym) {
        fetch('https://finnhub.io/api/v1/quote?symbol=' + encodeURIComponent(sym) +
              '&token=' + encodeURIComponent(cfg.finnhub_key))
          .then(function (r) { return r.json(); })
          .then(function (q) {
            if (q && isFinite(q.c) && q.c) push(fh[sym], q.c, q.pc || q.o || q.c);
          })
          .catch(function () {});
      });

      var fhRetry = 0;
      function connectFinnhub() {
        setConn('finnhub', '', 'finnhub: подключение');
        var ws;
        try {
          ws = new WebSocket('wss://ws.finnhub.io?token=' +
                             encodeURIComponent(cfg.finnhub_key));
        } catch (e) { return retryFinnhub(); }
        ws.onopen = function () {
          fhRetry = 0;
          setConn('finnhub', 'live', 'finnhub: онлайн');
          fhSymbols.forEach(function (sym) {
            ws.send(JSON.stringify({type: 'subscribe', symbol: sym}));
          });
        };
        ws.onmessage = function (ev) {
          var msg;
          try { msg = JSON.parse(ev.data); } catch (e) { return; }
          if (msg.type !== 'trade' || !msg.data) return;
          msg.data.forEach(function (t) {
            var label = fh[t.s];
            if (label) push(label, t.p);
          });
        };
        ws.onclose = retryFinnhub;
        ws.onerror = function () { try { ws.close(); } catch (e) {} };
      }
      function retryFinnhub() {
        setConn('finnhub', 'down', 'finnhub: нет связи');
        fhRetry++;
        setTimeout(connectFinnhub, Math.min(30000, 1000 * Math.pow(2, fhRetry)));
      }
      connectFinnhub();
    }
  }
})();
"""


# ════════════════════════════════════════════════════════════════════════════
#  РАЗМЕТКА
# ════════════════════════════════════════════════════════════════════════════

def _decimals(label: str, entry: dict) -> int:
    if "decimals" in entry:
        return int(entry["decimals"])
    up = label.upper()
    if up.startswith(("EUR", "GBP", "USD", "AUD", "NZD", "USDCHF")) and len(up) == 6:
        return 5
    if "DOGE" in up:
        return 5
    if "XRP" in up:
        return 4
    return 2


def render_live_panel(config: dict[str, Any], esc) -> tuple[str, dict[str, Any]]:
    """Возвращает (html, спецификация для движка)."""
    binance_map: dict[str, str] = {}
    finnhub_map: dict[str, str] = {}
    body: list[str] = []

    for group in config.get("groups", []):
        name = str(group.get("name", "")).strip()
        symbols = group.get("symbols") or []
        if not symbols:
            continue
        if name:
            body.append(f'<tr class="grp"><td colspan="4">{esc(name)}</td></tr>')
        for entry in symbols:
            if not isinstance(entry, dict):
                continue
            label = str(entry.get("label", "")).strip()
            if not label:
                continue
            dec = _decimals(label, entry)
            src = ""
            if entry.get("binance"):
                binance_map[str(entry["binance"]).upper()] = label
                src = "binance"
            elif entry.get("finnhub"):
                finnhub_map[str(entry["finnhub"])] = label
                src = "finnhub"
            note = str(entry.get("note", "")).strip()
            if not src and not note:
                note = "источник не задан"
            sub = f"<small>{esc(note)}</small>" if note else ""
            body.append(
                f'<tr class="idle" data-label="{esc(label)}" data-decimals="{dec}">'
                f'<td class="sym">{esc(label)}{sub}</td>'
                f'<td class="px">—</td>'
                f'<td class="chg hide-s">—</td>'
                f'<td class="pct">—</td></tr>')

    conn: list[str] = []
    if binance_map:
        conn.append('<span id="conn-binance">binance: подключение</span>')
    if finnhub_map:
        conn.append('<span id="conn-finnhub">finnhub: подключение</span>')

    spec = {
        "refresh_ms": int(config.get("refresh_ms", 1000)),
        "finnhub_key": str(config.get("finnhub_key", "")),
        "binance": binance_map,
        "finnhub": finnhub_map,
    }
    data = json.dumps(spec, separators=(",", ":"), ensure_ascii=False).replace('"', "&quot;")

    notes = ["Цены идут напрямую от источника в браузер. Файл нужно держать открытым — "
             "закроешь вкладку, поток оборвётся."]
    if finnhub_map and not spec["finnhub_key"]:
        notes.append("Для некриптовых символов впиши бесплатный ключ finnhub.io "
                     "в поле finnhub_key в live.json — без него они останутся серыми.")

    html = (
        '<section><div class="eyebrow"><span class="num">00</span>Live Quotes</div>'
        '<h2>Живые котировки</h2>'
        f'<div class="live-card" id="live-quotes" data-live="{data}">'
        '<div class="live-head"><span class="t">Терминал</span>'
        f'<span class="sub">обновление раз в {spec["refresh_ms"] / 1000:.0f} сек</span>'
        f'<span class="conn">{"".join(conn)}</span></div>'
        '<table class="live-table"><thead><tr>'
        '<th>Символ</th><th>Цена</th><th class="hide-s">Изм.</th><th>%</th>'
        '</tr></thead><tbody>' + "".join(body) + '</tbody></table>'
        f'<div class="live-note">{" ".join(esc(n) for n in notes)}</div>'
        '</div></section>')
    return html, spec
