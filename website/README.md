# BITFISH — ocean-style memecoin site

One self-contained page for the Solana token
`AzoyECzeEbmi3bczngZZnZDj4M9EreNM93mDXqNcpump`.

**`index.html` is the whole site** — markup, styles, scripts, the pixel-art
mascot (an inline SVG `<symbol>`), the fish family and the mini-game all live
in that single file. Nothing else is
required to publish it: drop it on any host, open it from a USB stick, send it
in a chat — it renders.

```
website/
├── index.html              # the site (~100 KB, no dependencies, no build step)
├── assets/img/             # standalone brand files, NOT needed by the page
│   ├── og.png              #   share card for X / Telegram previews
│   ├── fish.png            #   mascot at 16x for stickers and avatars
│   ├── fish.svg            #   mascot as vector
│   └── favicon.png         #   already embedded in index.html as a data URI
├── tools/make_assets.py    # redraws the mascot and re-embeds it into index.html
└── tools/verify_claim.py   # checks a signed reward ticket before you pay it
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

## The fish family — adding the next coin

Every coin in the family is the same pixel fish with a different palette and a
different glyph on its flank, so a new coin is **one entry**, no artwork:

```js
// in the second <script>, near the top
const FAMILY = [
  { id: "solfish", skin: "solfish", glyph: "sol",
    name: "SOLFISH", ticker: "$SOLFISH", chain: "Solana", status: "soon",
    line: "Green and violet, quick as a block." },
  ...
];
```

- `skin` — a palette from `SKINS` above it (`d` outline, `b`/`B`/`s` body,
  `c`/`C` fins, `w` eye, `r` lips, `g` glyph). Copy a line and change the
  colours to add one.
- `glyph` — a key from `FISH_GLYPHS`: `btc`, `sol`, `eth`, `au`, `ag`, `usd`,
  `quest`. New glyphs are 9×11 pixel strings in `GLYPHS` inside
  `tools/make_assets.py`; re-run it to push them into the page.
- `status` — `live`, `soon` or `planned`. `live` also gets a Buy button
  pointing at the contract in `CONFIG`.

The card in the gallery, the token inside the game and the fish drifting
across the page background all come from that entry — the game hands out every
coin except the `mystery` one, and the background school cycles through all of
them.

When a sibling coin actually launches it needs its own contract; the current
`CONFIG.contract` belongs to BITFISH only.

## Reward mode (play for $BFISH)

Runs only count for people who hold the coin. The page:

1. connects a Solana wallet (Phantom / Solflare) — read-only, no transaction,
2. reads the wallet's $BFISH balance from an RPC endpoint,
3. unlocks ×2 scoring and denser token drops above the threshold,
4. asks the wallet to sign a plain-text receipt at the end of a run and turns
   it into a **claim ticket** (base64) the player sends you.

```js
rewards: {
  minTokens: 100000,   // hold this much before runs count
  perPoint: 10,        // $BFISH per point
  cap: 25000,          // ceiling for a single run
  multiplier: 2,       // score multiplier while reward mode is on
  rpc: "https://api.mainnet-beta.solana.com",
},
```

**A static page cannot send tokens.** It can only prove *who* played and *what
they scored* — you pay the tickets out by hand (or feed the verified ones to
an airdrop script). The public RPC above is rate-limited; for real traffic put
a Helius/QuickNode/Triton endpoint in `rpc`.

Before paying anything, verify the ticket:

```bash
python3 tools/verify_claim.py <ticket>            # checks the signature
python3 tools/verify_claim.py <ticket> --balance  # ...and current holdings
cat tickets.txt | python3 tools/verify_claim.py - # a batch, one per line
```

It rebuilds the exact message the wallet signed and checks the ed25519
signature, so an edited score fails (`SIGNATURE DOES NOT MATCH`). Pure standard
library, nothing to install. Two things it cannot know: whether a ticket was
already paid, and whether one person is farming from several wallets — keep a
list of paid tickets (the `at` timestamp plus wallet is a good key) and set the
threshold high enough that farming costs more than it pays.

One warning worth taking seriously: "buy in, then earn tokens by playing" is a
promotional scheme, and in some jurisdictions that shape (paid entry, prize
out) is regulated. Keep the payouts discretionary — the copy on the page
already says so — and take advice before you scale it up.

## The mini-game

`Deep Dive` lives in the same file: swim the fish through red candles, eat the
family tokens, 5 points each. Space / W / ↑ / click / tap to swim, `P` to
pause; the best score is kept in `localStorage`. It pauses itself when it
scrolls out of view or the tab is hidden.

Tuning is at the top of the game block: `GRAVITY`, `FLAP`, `PIPE_SPACING`, and
inside `reset()` the starting `speed` (138) and `gap` (122), which get harder
by `+3.5` and `-1.6` per candle passed.

## Live price block

The four tiles poll the public DEX Screener API every 60 seconds:

```
https://api.dexscreener.com/latest/dex/tokens/<contract>
```

Before the token is indexed (or if the request fails) they stay at `—` and the
note underneath explains why — nothing breaks.

## Pixel art

`tools/make_assets.py` draws the mascot on a 46×32 grid, writes the files in
`assets/img/`, and rewrites two blocks inside `index.html`: the
`<symbol id="fish">` between the `<!-- fish:start -->` / `<!-- fish:end -->`
markers, and the raw pixel data the game and the family cards read, between
`/* fishdata:start */` and `/* fishdata:end */`:

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
  every transition switch off for visitors who ask for less motion. The game
  only runs after someone presses Start.
- The page is English only. Text sits directly in the markup — no translation
  layer to keep in sync.
