"""Offline meme maker: the bot's render pipeline without the cloud.

Same recipes as main.py/meme.py: P1 tone-matched background (gradient
fallback — image CDNs are unreachable from here), bias-free style pick,
random effect at the bot's 22% rate, P6 punchline override for re-rolls.

Zero network: background is always the local tone-matched gradient.
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

import meme

OUT_DIR = Path(__file__).parent / "memes_out"


def _list(bank):
    for e in bank:
        print(f"{e.id:22s} [{e.tone:5s}] {e.quote[:46]} → {e.punchline[:46]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="list bank entries and exit")
    ap.add_argument("--id", help="bank entry id (default: random)")
    ap.add_argument("--layout", choices=meme.LAYOUTS, help="force layout")
    ap.add_argument("--effect", choices=sorted(meme.EFFECTS), help="force effect")
    ap.add_argument("--punchline", help="override punchline (render-only, P6)")
    ap.add_argument("--seed", type=int, help="random seed for reproducible style")
    ap.add_argument("--out", default=str(OUT_DIR), help="output directory")
    args = ap.parse_args()

    bank = meme.load_bank()
    if args.list:
        _list(bank)
        return

    if args.seed is not None:
        random.seed(args.seed)
    entry = next((e for e in bank if e.id == args.id), None) if args.id \
        else random.choice(bank)
    if entry is None:
        sys.exit(f"no entry {args.id!r} in bank")
    if args.punchline:
        entry.punchline = args.punchline.strip()

    style = meme.pick_style(force_layout=args.layout)
    if args.effect:
        style.effect = args.effect
    elif not style.effect and random.random() < 0.22:
        style.effect = random.choice(list(meme.EFFECTS))

    img, img_source, bg_tag = meme._gradient_fallback(entry.tone)
    data = meme.render_meme(img, entry, style)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    eff = f"-{style.effect}" if style.effect else ""
    path = out / f"{entry.id}-{style.layout}{eff}.jpg"
    path.write_bytes(data)
    print(f"стиль: {style.layout}{eff} · фон: {img_source} · [{entry.tone}] · {path}")


if __name__ == "__main__":
    main()
