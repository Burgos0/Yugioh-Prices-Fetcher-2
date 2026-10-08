#!/usr/bin/env python3
"""
Generate Instagram posts (1080x1350 PNG per card + caption.txt) from top_gainers.json.

  python make_posts.py                 # shows the list, you type which cards to post
  python make_posts.py --pick 2,5,9    # skip the prompt
  python make_posts.py --top 3         # auto-pick the top 3 by % gain
  python make_posts.py --min-price 2   # hide cards under $2 (default $1)
  python make_posts.py --html-only     # write HTML only (debugging)
"""
import argparse
import base64
import json
import re
import sys
from datetime import date
from pathlib import Path

import requests
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parent

# ---- EDIT THESE ------------------------------------------------------------
SITE = "tcgtrending.com"
HANDLE = "@yourhandle"
AFFILIATE_URL = ""            # your TCGplayer affiliate link (goes in bio, not caption)
TITLE = "Market Watch"
WINDOW = "this week"
TITLE_FONT = '"Cinzel"'       # or '"Cinzel Decorative"' if Cinzel feels too clean
HASHTAGS = "#yugioh #yugiohtcg #yugiohcards #tcginvesting #cardprices #tcgplayer #yugiohinvesting"
# ----------------------------------------------------------------------------

IMG_SIZES = ["1000x1000", "400x400"]
HEADERS = {"User-Agent": "Mozilla/5.0 (tcgtrending social generator)"}


def b64(path, mime):
    return f"data:{mime};base64," + base64.b64encode(Path(path).read_bytes()).decode()


def font_css():
    f = ROOT / "assets" / "fonts"
    return (
        f'@font-face{{font-family:"Cinzel";font-weight:400 900;src:url({b64(f/"Cinzel.ttf","font/ttf")}) format("truetype");}}'
        f'@font-face{{font-family:"Cinzel Decorative";font-weight:700;src:url({b64(f/"CinzelDecorative-Bold.ttf","font/ttf")}) format("truetype");}}'
        f'@font-face{{font-family:"Cinzel Decorative";font-weight:900;src:url({b64(f/"CinzelDecorative-Black.ttf","font/ttf")}) format("truetype");}}'
    )


def find_data(arg):
    if arg:
        return Path(arg)
    for p in (ROOT.parent / "data" / "top_gainers.json",
              ROOT / "data" / "top_gainers.json",
              Path.cwd() / "data" / "top_gainers.json"):
        if p.exists():
            return p
    sys.exit("Can't find top_gainers.json. Pass --data path/to/top_gainers.json")


def placeholder(name):
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="580">'
        '<rect width="400" height="580" rx="14" fill="#1b2430"/>'
        '<text x="200" y="290" fill="#6b7a8a" font-family="Arial" font-size="22" '
        f'text-anchor="middle">{re.sub("[<>&]", "", name)[:28]}</text></svg>'
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def card_image(product_id, name, cache_dir):
    """Download TCGplayer card art once; return as a data URI."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    f = cache_dir / f"{product_id}.jpg"
    if not f.exists():
        for size in IMG_SIZES:
            try:
                r = requests.get(
                    f"https://product-images.tcgplayer.com/fit-in/{size}/{product_id}.jpg",
                    headers=HEADERS, timeout=15)
                if r.ok and r.headers.get("content-type", "").startswith("image"):
                    f.write_bytes(r.content)
                    break
            except requests.RequestException:
                pass
    if not f.exists():
        print(f"  ! no image for {name} ({product_id}), using placeholder")
        return placeholder(name)
    return b64(f, "image/jpeg")


def fmt_price(x):
    return f"{x:,.2f}"


def build_card(row, cache_dir):
    name = row["card_name"]
    n = len(name)
    pct = f"{row['percent_gain']:.0f}"
    return {
        "name": name,
        "name_class": "xxlong" if n > 40 else "xlong" if n > 32 else "long" if n > 22 else "",
        "set_name": row.get("set_name", ""),
        "printing": row.get("printing", ""),
        "pct": pct,
        "pct_class": "sm" if len(pct) >= 4 else "",
        "old": fmt_price(row["baseline_value"]),
        "new": fmt_price(row["current_value"]),
        "img": card_image(row["product_id"], name, cache_dir),
    }


def parse_pick(text, max_n):
    picks = []
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            picks += list(range(int(m[1]), int(m[2]) + 1))
        elif part.isdigit():
            picks.append(int(part))
        else:
            sys.exit(f"Bad selection: {part!r}")
    bad = [p for p in picks if not 1 <= p <= max_n]
    if bad:
        sys.exit(f"Out of range: {bad} (list is 1-{max_n})")
    seen, out = set(), []
    for p in picks:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def make_caption(cards):
    lines = [f"Yu-Gi-Oh! price movers {WINDOW} \U0001F4C8", ""]
    for i, c in enumerate(cards, 1):
        lines.append(f"{i}. {c['name']} \u2014 +{c['pct']}% (${c['old']} \u2192 ${c['new']})")
    lines += [
        "",
        f"Full weekly gainers list: {SITE} (link in bio)",
        "Follow for a new list every week.",
        "",
        "Prices from TCGplayer market data; not financial advice. "
        "Link in bio is an affiliate link, I may earn a commission.",
        "",
        HASHTAGS,
    ]
    return "\n".join(lines)


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


def ordinal_date(d):
    n = d.day
    suf = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf} {d.strftime('%B')}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data")
    ap.add_argument("--top", type=int, help="auto-pick the top N, skip prompt")
    ap.add_argument("--pick", help="list numbers, e.g. 1,3,5-7")
    ap.add_argument("--min-price", type=float, default=1.0, help="min current price (default 1.0)")
    ap.add_argument("--show", type=int, default=25, help="how many rows to list")
    ap.add_argument("--html-only", action="store_true")
    args = ap.parse_args()

    data = json.loads(find_data(args.data).read_text())
    data = [r for r in data if r["current_value"] >= args.min_price]
    data.sort(key=lambda r: r["percent_gain"], reverse=True)
    data = data[: args.show]
    if not data:
        sys.exit("No cards after filtering.")

    if args.top:
        chosen = data[: args.top]
    else:
        if not args.pick:
            print(f"\n{'#':>3}  {'Card':<42} {'Set':<30} {'Old':>8} {'New':>8} {'%':>6}")
            for i, r in enumerate(data, 1):
                print(f"{i:>3}  {r['card_name'][:41]:<42} {r['set_name'][:29]:<30} "
                      f"{r['baseline_value']:>8.2f} {r['current_value']:>8.2f} "
                      f"{r['percent_gain']:>5.0f}%")
            args.pick = input("\nPick cards (e.g. 2,5,9), in the order you want slides: ")
        chosen = [data[i - 1] for i in parse_pick(args.pick, len(data))]
    chosen = chosen[:10]  # IG carousel max 10 slides
    if not chosen:
        sys.exit("Nothing selected.")

    today = date.today()
    out = ROOT / "out" / today.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    cache = ROOT / "out" / ".img_cache"

    print(f"\nFetching card art for {len(chosen)} cards...")
    cards = [build_card(r, cache) for r in chosen]

    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=True)
    css = font_css() + (ROOT / "templates" / "base.css").read_text().replace("__TITLE_FONT__", TITLE_FONT)
    ctx = {
        "css": css, "site": SITE, "window": WINDOW, "title": TITLE,
        "date_str": ordinal_date(today),
        "bg": b64(ROOT / "assets" / "background.png", "image/png"),
    }

    slides = []
    for i, c in enumerate(cards, 1):
        slides.append((f"{i:02d}_{slugify(c['name'])}", env.get_template("card.html").render(c=c, **ctx)))

    if args.html_only:
        for name, html in slides:
            (out / f"{name}.html").write_text(html)
    else:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1080, "height": 1350})
            for name, html in slides:
                page.set_content(html, wait_until="load")
                page.evaluate("document.fonts.ready")
                page.screenshot(path=str(out / f"{name}.png"))
                print(f"  wrote {name}.png")
            browser.close()

    (out / "caption.txt").write_text(make_caption(cards))
    print(f"\nDone. {len(slides)} slides + caption.txt in {out}")
    if AFFILIATE_URL:
        print(f"Bio link: {AFFILIATE_URL}")


if __name__ == "__main__":
    main()
