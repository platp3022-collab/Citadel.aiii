#!/usr/bin/env python3
"""Generates the pixel-art mascot assets for the site.

Everything is drawn on a small integer grid and then exported both as SVG
(run-length merged rects, so it stays tiny and crisp) and as PNG
(nearest-neighbour upscale, written with zlib only - no third-party deps).

Run:  python3 tools/make_assets.py

It also re-embeds the mascot into index.html, between the <!-- fish:start -->
and <!-- fish:end --> markers, so the single-file page picks up any edit made
to the palette or to draw_fish().
"""

import os
import re
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, "..", "assets", "img"))

# ---------------------------------------------------------------- palette ---
PAL = {
    "d": "#111d3a",  # dark navy outline / bitcoin glyph
    "b": "#e3a01e",  # body base gold
    "B": "#f6cd5f",  # body highlight
    "s": "#bd7a12",  # body shadow
    "c": "#3fb2e6",  # fin cyan
    "C": "#8fe4ff",  # fin highlight
    "w": "#ffffff",  # eye white
    "r": "#e2564d",  # lips
    "g": "#16305c",  # background grid line
    "k": "#0a1730",  # background fill
}

W, H = 46, 32
CX, CY = 25.0, 16.0
RX, RY = 11.5, 7.2


# Every coin in the family is the same fish with a different glyph on its
# flank, so the glyphs live here and get exported to the page for the game.
GLYPH_POS = (19, 10)

GLYPHS = {
    "btc": ["..##.##..",
            "..##.##..",
            ".#######.",
            ".##...##.",
            ".##...##.",
            ".#######.",
            ".##...##.",
            ".##...##.",
            ".#######.",
            "..##.##..",
            "..##.##.."],
    "sol": [".........",
            ".#######.",
            "..#######",
            ".........",
            "#######..",
            ".#######.",
            ".........",
            "..#######",
            ".#######.",
            ".........",
            "........."],
    "eth": ["....#....",
            "...###...",
            "..#####..",
            ".#######.",
            "#########",
            ".#######.",
            "..#####..",
            "...###...",
            "....#....",
            ".........",
            "........."],
    "au":  [".........",
            ".........",
            ".........",
            ".##......",
            "#..#.#..#",
            "####.#..#",
            "#..#.#..#",
            "#..#..###",
            ".........",
            ".........",
            "........."],
    "ag":  [".........",
            ".........",
            ".........",
            ".##......",
            "#..#..###",
            "####.#..#",
            "#..#..###",
            "#..#....#",
            "......##.",
            ".........",
            "........."],
    "usd": ["....#....",
            "..#####..",
            ".#..#..#.",
            ".#..#....",
            "..####...",
            "....#.##.",
            "....#..#.",
            ".#..#..#.",
            "..#####..",
            "....#....",
            "........."],
    "quest": ["..#####..",
              ".##...##.",
              "......##.",
              ".....##..",
              "....##...",
              "...##....",
              "...##....",
              ".........",
              "...##....",
              "...##....",
              "........."],
}


def blank(w=W, h=H):
    return [[None] * w for _ in range(h)]


def put(grid, x, y, ch, overwrite=True):
    if 0 <= x < len(grid[0]) and 0 <= y < len(grid):
        if overwrite or grid[y][x] is None:
            grid[y][x] = ch


def in_body(x, y):
    return ((x + 0.5 - CX) / RX) ** 2 + ((y + 0.5 - CY) / RY) ** 2 <= 1.0


def draw_fish(glyph="btc"):
    g = blank()

    # --- tail: forked V shape on the left ------------------------------
    for x in range(5, 17):
        t = 16 - x
        outer = 1.6 + t * 1.05
        inner = t * 0.32
        for y in range(H):
            dy = abs(y + 0.5 - CY)
            if inner <= dy <= outer:
                put(g, x, y, "c")

    # --- dorsal fin (top) ----------------------------------------------
    for y in range(3, 10):
        half = 1.0 + (y - 3) * 1.2
        for x in range(int(25 - half), int(25 + half) + 1):
            if not in_body(x, y):
                put(g, x, y, "c")

    # --- anal fin (bottom) ---------------------------------------------
    for y in range(23, 29):
        half = 1.0 + (28 - y) * 1.15
        for x in range(int(23 - half), int(23 + half) + 1):
            if not in_body(x, y):
                put(g, x, y, "c")

    # --- fin highlights (diagonal shine, like the reference art) -------
    for y in range(H):
        for x in range(W):
            if g[y][x] == "c" and (x + y) % 7 in (0, 1):
                g[y][x] = "C"

    # --- body -----------------------------------------------------------
    for y in range(H):
        for x in range(W):
            if in_body(x, y):
                dy = y + 0.5 - CY
                if dy < -3.2:
                    g[y][x] = "B"
                elif dy > 3.6:
                    g[y][x] = "s"
                else:
                    g[y][x] = "b"

    # diagonal sheen across the body
    for y in range(H):
        for x in range(W):
            if g[y][x] in ("b", "s") and (x - y) % 11 in (0, 1) and x < CX + 7:
                g[y][x] = "B" if g[y][x] == "b" else "b"

    # --- pectoral fin (under the head) ----------------------------------
    for y in range(21, 26):
        half = (25 - y) * 1.0
        for x in range(int(30 - half), int(30 + half) + 1):
            if not in_body(x, y):
                put(g, x, y, "c" if (x + y) % 7 else "C")

    # --- coin glyph on the flank ----------------------------------------
    if glyph:
        gx, gy = GLYPH_POS
        for j, row in enumerate(GLYPHS[glyph]):
            for i, ch in enumerate(row):
                if ch == "#" and in_body(gx + i, gy + j):
                    put(g, gx + i, gy + j, "d")

    # --- eye --------------------------------------------------------------
    for y in range(12, 16):
        for x in range(28, 32):
            put(g, x, y, "w")
    for y in range(13, 16):
        for x in range(29, 31):
            put(g, x, y, "d")

    # --- lips -------------------------------------------------------------
    for x in range(33, 36):
        put(g, x, 20, "r")
    put(g, 35, 19, "r")

    # --- outline ----------------------------------------------------------
    solid = [[g[y][x] is not None for x in range(W)] for y in range(H)]
    for y in range(H):
        for x in range(W):
            if solid[y][x]:
                continue
            near = False
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < W and 0 <= ny < H and solid[ny][nx]:
                    near = True
                    break
            if near:
                g[y][x] = "d"
    return g


def scene(fish, w=76, h=40, ox=15, oy=4):
    """Fish on the dark grid background (used for og:image + favicon)."""
    g = blank(w, h)
    for y in range(h):
        for x in range(w):
            g[y][x] = "g" if (x % 9 == 0 or y % 9 == 0) else "k"
    bubbles = [(4, 6), (11, 24), (7, 34), (21, 9), (30, 37), (44, 3),
               (63, 28), (68, 12), (71, 35), (52, 33), (37, 2), (58, 6)]
    for bx, by in bubbles:
        put(g, bx, by, "c")
    for y in range(H):
        for x in range(W):
            if fish[y][x]:
                put(g, ox + x, oy + y, fish[y][x])
    return g


# ------------------------------------------------------------------ export ---
def to_svg(g, scale=1, bg=None):
    h, w = len(g), len(g[0])
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
        f'width="{w * scale}" height="{h * scale}" shape-rendering="crispEdges">'
    ]
    if bg:
        parts.append(f'<rect width="{w}" height="{h}" fill="{bg}"/>')
    for y, row in enumerate(g):
        x = 0
        while x < w:
            ch = row[x]
            if ch is None:
                x += 1
                continue
            run = 1
            while x + run < w and row[x + run] == ch:
                run += 1
            parts.append(
                f'<rect x="{x}" y="{y}" width="{run}" height="1" fill="{PAL[ch]}"/>'
            )
            x += run
    parts.append("</svg>")
    return "".join(parts)


def rgba(ch):
    if ch is None:
        return (0, 0, 0, 0)
    hexv = PAL[ch].lstrip("#")
    return (int(hexv[0:2], 16), int(hexv[2:4], 16), int(hexv[4:6], 16), 255)


def to_png(g, path, scale=1):
    h, w = len(g), len(g[0])
    raw = bytearray()
    for y in range(h):
        for _ in range(scale):
            raw.append(0)  # filter type 0
            for x in range(w):
                px = bytes(rgba(g[y][x]))
                raw.extend(px * scale)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w * scale, h * scale, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(png)


def grid_rows(g):
    """The grid as one string per row - the format the page's game reads."""
    return ["".join(ch if ch else "." for ch in row) for row in g]


def embed_game_data(page):
    """Refresh the shared pixel data inside index.html (game + fish family)."""
    plain = draw_fish(glyph=None)          # no glyph: the page stamps its own
    rows = ",\n  ".join('"%s"' % r for r in grid_rows(plain))
    glyphs = ",\n  ".join(
        '%s: [%s]' % (k, ", ".join('"%s"' % r for r in v))
        for k, v in GLYPHS.items()
    )
    block = ("/* fishdata:start */\n"
             "const FISH_W = %d, FISH_H = %d;\n"
             "const FISH_GLYPH_POS = { x: %d, y: %d };\n"
             "const FISH_MAP = [\n  %s\n];\n"
             "const FISH_GLYPHS = {\n  %s\n};\n"
             "/* fishdata:end */") % (W, H, GLYPH_POS[0], GLYPH_POS[1], rows, glyphs)
    html = open(page).read()
    new = re.sub(r"/\* fishdata:start \*/.*?/\* fishdata:end \*/",
                 lambda m: block, html, flags=re.S)
    if new != html:
        open(page, "w").write(new)
        print("embedded pixel data into", page)


def embed_in_page(fish):
    """Replace the <symbol id="fish"> block inside the single-file index.html."""
    page = os.path.normpath(os.path.join(HERE, "..", "index.html"))
    if not os.path.exists(page):
        return
    inner = to_svg(fish).split(">", 1)[1].rsplit("</svg>", 1)[0]
    block = ('<!-- fish:start -->\n'
             '<svg width="0" height="0" style="position:absolute" aria-hidden="true">\n'
             '  <symbol id="fish" viewBox="0 0 %d %d" shape-rendering="crispEdges">' % (W, H)
             + inner + '</symbol>\n</svg>\n<!-- fish:end -->')
    html = open(page).read()
    new = re.sub(r"<!-- fish:start -->.*?<!-- fish:end -->", lambda m: block, html, flags=re.S)
    if new != html:
        open(page, "w").write(new)
        print("embedded mascot into", page)


def main():
    os.makedirs(OUT, exist_ok=True)
    fish = draw_fish()

    with open(os.path.join(OUT, "fish.svg"), "w") as fh:
        fh.write(to_svg(fish, scale=12))
    to_png(fish, os.path.join(OUT, "fish.png"), scale=16)

    card = scene(fish)
    to_png(card, os.path.join(OUT, "og.png"), scale=16)

    icon = scene(fish, w=56, h=56, ox=5, oy=12)
    to_png(icon, os.path.join(OUT, "favicon.png"), scale=4)

    embed_in_page(fish)
    page = os.path.normpath(os.path.join(HERE, "..", "index.html"))
    if os.path.exists(page):
        embed_game_data(page)
    print("wrote assets to", OUT)


if __name__ == "__main__":
    main()
