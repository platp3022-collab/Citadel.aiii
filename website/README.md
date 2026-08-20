# BITFISH — ocean-style memecoin site

One self-contained page for the Solana token
`AzoyECzeEbmi3bczngZZnZDj4M9EreNM93mDXqNcpump`.

**`index.html` is the whole site** — markup, styles, scripts and the pixel-art
mascot (an inline SVG `<symbol>`) live in that single file. Nothing else is
required to publish it: drop it on any host, open it from a USB stick, send it
in a chat — it renders.

```
website/
├── index.html              # the site (~74 KB, no dependencies, no build step)
├── assets/img/             # standalone brand files, NOT needed by the page
│   ├── og.png              #   share card for X / Telegram previews
│   ├── fish.png            #   mascot at 16x for stickers and avatars
│   ├── fish.svg            #   mascot as vector
│   └── favicon.png         #   already embedded in index.html as a data URI
└── tools/make_assets.py    # redraws the mascot and re-embeds it into index.html
```

The only external request the page makes is the Google Fonts stylesheet (pixel
font "Press Start 2P" + Inter). Offline, it falls back to system fonts and
still looks right — delete the two `<link ...fonts...>` lines if you want zero
outside calls.

## What to edit

Everything project-specific sits in one `CONFIG` block near the bottom of
`index.html`, inside `<script>`:

```js
const CONFIG = {
  name: "BITFISH",
  ticker: "$BFISH",
  contract: "AzoyECzeEbmi3bczngZZnZDj4M9EreNM93mDXqNcpump",
  chain: "solana",
  twitter: "https://x.com/",     // <- put the real handle here
  telegram: "https://t.me/",     // <- and the real chat here
};
```

The pump.fun, DEX Screener, Jupiter and Solscan links are built from the
contract address automatically.

Renaming the coin: change `CONFIG`, then the plain-text mentions —
`sed -i 's/BITFISH/YOURNAME/g; s/\$BFISH/\$YOURTICKER/g' index.html` covers the
title, meta tags, hero copy and disclaimer.

## Two languages

Every translatable element carries `data-en` and `data-ru`. The EN/RU switch in
the header swaps them and remembers the choice in `localStorage`; visitors with
a Russian/Ukrainian browser locale get RU on the first visit. To add a phrase,
give the element both attributes — the visible text stays as the fallback.

## Live price block

The four tiles poll the public DEX Screener API every 60 seconds:

```
https://api.dexscreener.com/latest/dex/tokens/<contract>
```

Before the token is indexed (or if the request fails) they stay at `—` and the
note underneath explains why — nothing breaks.

## Pixel art

`tools/make_assets.py` draws the mascot on a 46×32 grid, writes the files in
`assets/img/`, and rewrites the `<symbol id="fish">` block inside `index.html`
between the `<!-- fish:start -->` / `<!-- fish:end -->` markers:

```bash
python3 tools/make_assets.py     # python3 only, no packages needed
```

Colors are in the `PAL` dict, the shape in `draw_fish()`.

## Deploy

- **GitHub Pages** — Settings → Pages → deploy from branch, folder `/website`.
- **Netlify / Vercel / Cloudflare Pages** — publish directory `website`, build
  command empty.
- **Anything else** — upload `index.html`. That is the entire site.

For link previews in X and Telegram, upload `assets/img/og.png` too and
uncomment the `og:image` meta tag in `<head>` with its absolute URL
(`https://yourdomain/og.png`) — scrapers need a real URL, not an embedded image.

## Notes

- The tokenomics section describes the standard pump.fun launch (1B supply,
  mint revoked, LP burned on bonding-curve completion, 0% tax). Verify it
  against the contract before publishing; if anything differs, edit those four
  cards.
- The page respects `prefers-reduced-motion`: bubbles, the drifting school and
  every transition switch off for visitors who ask for less motion.
