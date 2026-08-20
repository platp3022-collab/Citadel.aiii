/* =========================================================================
   BITFISH - site logic
   -------------------------------------------------------------------------
   Everything you normally need to change lives in CONFIG below.
   ========================================================================= */
const CONFIG = {
  name: "BITFISH",
  ticker: "$BFISH",
  contract: "AzoyECzeEbmi3bczngZZnZDj4M9EreNM93mDXqNcpump",
  chain: "solana",
  // Social links - replace with the real ones when they exist.
  twitter: "https://x.com/",
  telegram: "https://t.me/",
};

const LINKS = {
  pump: `https://pump.fun/coin/${CONFIG.contract}`,
  dexscreener: `https://dexscreener.com/${CONFIG.chain}/${CONFIG.contract}`,
  jupiter: `https://jup.ag/swap/SOL-${CONFIG.contract}`,
  solscan: `https://solscan.io/token/${CONFIG.contract}`,
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ------------------------------------------------------------- content -- */
function applyConfig() {
  $$("[data-token='ticker']").forEach((el) => (el.textContent = CONFIG.ticker));
  $$("[data-token='name']").forEach((el) => (el.textContent = CONFIG.name));
  $$("#caText, #caText2").forEach((el) => (el.textContent = CONFIG.contract));

  const set = (id, href) => { const el = $(id); if (el) el.href = href; };
  set("#buyPump", LINKS.pump);
  set("#buyPump2", LINKS.pump);
  set("#socialPump", LINKS.pump);
  set("#chartLink", LINKS.dexscreener);
  set("#socialDex", LINKS.dexscreener);
  set("#jupLink", LINKS.jupiter);
  set("#solscanLink", LINKS.solscan);
  set("#socialX", CONFIG.twitter);
  set("#socialTg", CONFIG.telegram);

  const year = $("#year");
  if (year) year.textContent = new Date().getFullYear();
}

/* ------------------------------------------------------------ language -- */
let statsState = null; // "live" | "pending" - so the note can follow the language

function setLang(lang) {
  document.documentElement.lang = lang;
  $$("[data-en]").forEach((el) => {
    const text = el.getAttribute(lang === "ru" ? "data-ru" : "data-en");
    if (text !== null && text !== "") el.textContent = text;
  });
  $$(".lang button").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.lang === lang))
  );
  if (statsState) renderStatsNote(statsState.kind, statsState.time);
  try { localStorage.setItem("bitfish-lang", lang); } catch (e) { /* private mode */ }
}

function initLang() {
  let saved = null;
  try { saved = localStorage.getItem("bitfish-lang"); } catch (e) { /* ignore */ }
  const guess = /^(ru|uk|be|kk)/i.test(navigator.language || "") ? "ru" : "en";
  setLang(saved || guess);
  $$(".lang button").forEach((b) =>
    b.addEventListener("click", () => setLang(b.dataset.lang))
  );
}

/* ---------------------------------------------------------------- toast -- */
let toastTimer;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
}

/* ------------------------------------------------------------ copy CA --- */
async function copyContract() {
  const done = () =>
    toast(document.documentElement.lang === "ru"
      ? "Адрес контракта скопирован"
      : "Contract address copied");
  try {
    await navigator.clipboard.writeText(CONFIG.contract);
    done();
  } catch (e) {
    // clipboard API is blocked on http:// and in some in-app browsers
    const tmp = document.createElement("textarea");
    tmp.value = CONFIG.contract;
    tmp.setAttribute("readonly", "");
    tmp.style.cssText = "position:absolute;left:-9999px";
    document.body.appendChild(tmp);
    tmp.select();
    try { document.execCommand("copy"); done(); } catch (_) { toast(CONFIG.contract); }
    document.body.removeChild(tmp);
  }
}

/* ------------------------------------------------------------ live data -- */
const fmtUsd = (n) => {
  if (!isFinite(n)) return "—";
  if (n >= 1e9) return "$" + (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return "$" + (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return "$" + (n / 1e3).toFixed(1) + "K";
  return "$" + n.toFixed(2);
};

const fmtPrice = (p) => {
  const n = parseFloat(p);
  if (!isFinite(n) || n === 0) return "—";
  if (n >= 1) return "$" + n.toFixed(3);
  // show the first 3 significant digits for tiny memecoin prices
  const decimals = Math.min(12, Math.max(4, Math.ceil(-Math.log10(n)) + 2));
  return "$" + n.toFixed(decimals);
};

function renderStatsNote(kind, time) {
  const note = $("#statsNote");
  const ru = document.documentElement.lang === "ru";
  statsState = { kind, time };
  note.removeAttribute("data-en");
  note.removeAttribute("data-ru");
  if (kind === "live") {
    note.textContent = ru
      ? "Данные DEX Screener · обновлено " + time
      : "Live from DEX Screener · updated " + time;
  } else {
    note.textContent = ru
      ? "Статистика появится, как только токен проиндексирует DEX Screener. Пока смотри график по ссылке выше."
      : "Stats appear as soon as the token is indexed by DEX Screener. Until then, use the chart link above.";
  }
}

async function loadStats() {
  try {
    const res = await fetch(
      `https://api.dexscreener.com/latest/dex/tokens/${CONFIG.contract}`,
      { headers: { accept: "application/json" } }
    );
    if (!res.ok) throw new Error("http " + res.status);
    const data = await res.json();
    const pairs = (data && data.pairs) || [];
    if (!pairs.length) throw new Error("no pairs");

    const pair = pairs.reduce((best, p) =>
      (p.liquidity?.usd || 0) > (best.liquidity?.usd || 0) ? p : best, pairs[0]);

    $("#statPrice").textContent = fmtPrice(pair.priceUsd);
    $("#statMcap").textContent = fmtUsd(pair.marketCap || pair.fdv);
    $("#statVol").textContent = fmtUsd(pair.volume?.h24);

    const ch = parseFloat(pair.priceChange?.h24);
    const chEl = $("#statChange");
    if (isFinite(ch)) {
      chEl.textContent = (ch > 0 ? "+" : "") + ch.toFixed(2) + "%";
      chEl.style.color = ch >= 0 ? "#5ce08a" : "#e2564d";
    }

    renderStatsNote("live", new Date().toLocaleTimeString());
  } catch (e) {
    renderStatsNote("pending");
  }
}

/* --------------------------------------------------------------- bubbles -- */
function initBubbles() {
  const canvas = $("#bubbles");
  if (!canvas || reduced) return;
  const ctx = canvas.getContext("2d");
  let w = 0, h = 0, dpr = 1, bubbles = [];

  const spawn = (initial) => ({
    x: Math.random() * w,
    y: initial ? Math.random() * h : h + 20 + Math.random() * 60,
    r: 1.5 + Math.random() * 5,
    speed: 0.25 + Math.random() * 0.9,
    drift: (Math.random() - 0.5) * 0.5,
    phase: Math.random() * Math.PI * 2,
    alpha: 0.15 + Math.random() * 0.4,
  });

  const resize = () => {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = canvas.clientWidth = window.innerWidth;
    h = canvas.clientHeight = window.innerHeight;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const count = Math.round(Math.min(90, Math.max(28, w / 16)));
    bubbles = Array.from({ length: count }, () => spawn(true));
  };

  const frame = () => {
    ctx.clearRect(0, 0, w, h);
    for (const b of bubbles) {
      b.y -= b.speed;
      b.phase += 0.02;
      b.x += b.drift + Math.sin(b.phase) * 0.4;
      if (b.y + b.r < -10) Object.assign(b, spawn(false));

      ctx.beginPath();
      ctx.arc(b.x, b.y, b.r, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(143, 228, 255, ${b.alpha * 0.35})`;
      ctx.fill();
      ctx.strokeStyle = `rgba(190, 245, 255, ${b.alpha})`;
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(b.x - b.r * 0.3, b.y - b.r * 0.3, Math.max(0.5, b.r * 0.22), 0, Math.PI * 2);
      ctx.fillStyle = `rgba(255,255,255,${b.alpha * 0.8})`;
      ctx.fill();
    }
    requestAnimationFrame(frame);
  };

  window.addEventListener("resize", resize);
  resize();
  frame();
}

/* ---------------------------------------------------------------- school -- */
function initSchool() {
  const box = $("#school");
  if (!box || reduced) return;
  const fish = [
    { top: 12, size: 66, dur: 52, delay: -6, hue: -12, op: 0.22, blur: 1.2 },
    { top: 45, size: 44, dur: 74, delay: -26, hue: 18, op: 0.16, blur: 1.8 },
    { top: 68, size: 92, dur: 42, delay: -14, hue: -25, op: 0.26, blur: 0.6 },
    { top: 88, size: 38, dur: 84, delay: -38, hue: 10, op: 0.14, blur: 2 },
  ];
  fish.forEach((f) => {
    const img = document.createElement("img");
    img.src = "assets/img/fish.svg";
    img.alt = "";
    img.style.top = f.top + "vh";
    img.style.width = f.size + "px";
    img.style.opacity = f.op;
    img.style.animationDuration = f.dur + "s";
    img.style.animationDelay = f.delay + "s";
    img.style.filter = `hue-rotate(${f.hue}deg) blur(${f.blur}px) drop-shadow(0 6px 18px rgba(0,0,0,.45))`;
    box.appendChild(img);
  });
}

/* --------------------------------------------------------------- reveal -- */
function initReveal() {
  const items = $$(".reveal");
  if (!("IntersectionObserver" in window) || reduced) {
    items.forEach((el) => el.classList.add("in"));
    return;
  }
  const io = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry, i) => {
        if (entry.isIntersecting) {
          setTimeout(() => entry.target.classList.add("in"), i * 70);
          io.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.12, rootMargin: "0px 0px -40px 0px" }
  );
  items.forEach((el) => io.observe(el));
}

/* ------------------------------------------------------------------ nav -- */
function initNav() {
  const burger = $("#burger");
  const links = $("#navLinks");
  burger.addEventListener("click", () => {
    const open = links.classList.toggle("open");
    burger.setAttribute("aria-expanded", String(open));
    burger.textContent = open ? "✕" : "☰";
  });
  $$("#navLinks a").forEach((a) =>
    a.addEventListener("click", () => {
      links.classList.remove("open");
      burger.setAttribute("aria-expanded", "false");
      burger.textContent = "☰";
    })
  );
}

/* ----------------------------------------------------------------- boot -- */
document.addEventListener("DOMContentLoaded", () => {
  applyConfig();
  initLang();
  initNav();
  initReveal();
  initBubbles();
  initSchool();
  $$("#copyCa, #copyCa2").forEach((b) => b.addEventListener("click", copyContract));
  $$("#caText, #caText2").forEach((c) => {
    c.style.cursor = "pointer";
    c.addEventListener("click", copyContract);
  });
  loadStats();
  setInterval(loadStats, 60000);
});
