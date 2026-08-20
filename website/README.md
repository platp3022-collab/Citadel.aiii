# BITFISH — ocean-style memecoin site

Static one-page site for the Solana token
`AzoyECzeEbmi3bczngZZnZDj4M9EreNM93mDXqNcpump`.
No build step, no dependencies — plain HTML, CSS and one JS file.

```
website/
├── index.html              # the whole page (EN + RU text in data-* attributes)
├── assets/
│   ├── css/style.css       # ocean theme
│   ├── js/app.js           # CONFIG, language switch, copy CA, live stats, bubbles
│   └── img/                # generated pixel art (fish, favicon, og image)
└── tools/make_assets.py    # regenerates the pixel art (python3, no deps)
```

## Run locally

```bash
cd website
python3 -m http.server 8080
# open http://localhost:8080
```

`file://` works too, but the clipboard button and the live price fetch behave
better over http.

## What to edit

Everything project-specific sits at the top of `assets/js/app.js`:

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
contract address automatically — no need to touch them.

If the coin has a different name than BITFISH / $BFISH, change `CONFIG` **and**
the plain-text mentions in `index.html` (title, meta description, hero text,
footer disclaimer). A quick `sed -i 's/BITFISH/YOURNAME/g; s/\$BFISH/\$YOURTICKER/g' index.html`
covers all of them.

## Two languages

Every translatable element carries `data-en` and `data-ru`. The EN/RU switch in
the header swaps them and remembers the choice in `localStorage`; first-time
visitors with a Russian/Ukrainian browser locale get RU automatically.
To add a phrase, add both attributes — the visible text is the fallback.

## Live price block

The four stat tiles pull the price, market cap, 24h volume and 24h change from
the public DEX Screener API every 60 seconds:

```
https://api.dexscreener.com/latest/dex/tokens/<contract>
```

Before the token is indexed (or if the request fails) the tiles stay at `—` and
the note under them explains why — nothing breaks.

## Pixel art

`tools/make_assets.py` draws the mascot on a 46×32 grid and exports:

| file | what it is |
|------|------------|
| `assets/img/fish.svg` | mascot, transparent, used everywhere on the page |
| `assets/img/fish.png` | same at 16× for socials / stickers |
| `assets/img/og.png` | 1216×640 share card (fish on the grid background) |
| `assets/img/favicon.png` | tab icon |

```bash
python3 tools/make_assets.py
```

Colors live in the `PAL` dict, the shape in `draw_fish()`.

## Deploy

**GitHub Pages** — Settings → Pages → Deploy from branch, then pick the branch
and the `/website` folder. Nothing else to configure.

**Netlify / Vercel / Cloudflare Pages** — publish directory `website`, build
command empty.

**Any static host** — upload the contents of `website/` as-is.

After deploying, set the absolute OG image URL in `index.html`
(`<meta property="og:image">`) to `https://yourdomain/assets/img/og.png` so
Twitter and Telegram render the preview card.

## Notes

- The tokenomics section describes the standard pump.fun launch (1B supply,
  mint revoked, LP burned on bonding-curve completion, 0% tax). Verify it
  against the contract before publishing — if anything differs, edit the four
  cards in `index.html`.
- The site respects `prefers-reduced-motion`: bubbles, the swimming school and
  all transitions turn off for visitors who ask for less motion.
