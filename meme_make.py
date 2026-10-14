"""Meme maker: the bot's render pipeline, runnable without the server.

Same recipes as main.py/meme.py: P1 tone-matched background (gradient
fallback — image CDNs are unreachable from here), bias-free style pick,
random effect at the bot's 22% rate, P6 punchline override for re-rolls.

With --glm the LLM steps run against z.ai too: P4 coherence check
(one re-roll... well, a warning — the background is always the local
gradient) and P6 punchline variants, using the exact prompts.py recipes.
Backgrounds stay zero-network on purpose.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

import meme
import prompts

OUT_DIR = Path(__file__).parent / "memes_out"
GLM_URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"


def _glm(model: str, system: str, user: str, *, max_tokens: int,
         temperature: float) -> str:
    key = os.getenv("GLM_API_KEY")
    if not key:
        sys.exit("GLM_API_KEY is not set (put it in .env)")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "thinking": {"type": "disabled"},  # glm-5.x burns the budget on reasoning
    }
    last = ""
    for attempt in range(2):
        try:
            r = httpx.post(
                GLM_URL, json=payload, timeout=120,
                headers={"Authorization": f"Bearer {key}"},
            )
            r.raise_for_status()
            data = r.json()
            content = ((data.get("choices") or [{}])[0].get("message") or {}) \
                .get("content") or ""
            if content.strip():
                return content
            last = "empty content"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        print(f"GLM attempt {attempt + 1} failed: {last}", file=sys.stderr)
    sys.exit(f"GLM failed: {last}")


def _variants(entry: meme.Entry, model: str) -> list[str]:
    bank = meme.load_bank()
    examples = random.sample(bank, min(4, len(bank)))
    ex_block = "\n".join(f"- «{e.quote}» → {e.punchline}" for e in examples)
    raw = _glm(
        model, prompts.MEME_PUNCHLINE_VARIANTS_SYSTEM,
        f"Примеры стиля канала:\n{ex_block}\n\n"
        f"Цитата: {entry.quote}\nТекущий панчлайн: {entry.punchline}\n"
        f"Тон: {entry.tone}\nОтвет — СТРОГО JSON-массив из 3 объектов.",
        max_tokens=800, temperature=0.9,
    )
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        sys.exit(f"GLM returned no JSON array: {raw[:200]}")
    items = json.loads(m.group(0))
    return [v.get("punchline", "") for v in items
            if isinstance(v, dict) and v.get("punchline", "").strip()][:3]


def _coherence(res: dict, model: str) -> tuple[int, str]:
    raw = _glm(
        model, prompts.MEME_COHERENCE_SYSTEM,
        f"Цитата: {res['quote']}\nПанчлайн: {res['punchline']}\n"
        f"Тон: {res['tone']}\nТег фона: {res.get('bg_tag', '?')}\n"
        f"Лэйаут: {res['layout']}\nФон из: {res.get('img_source', '?')}",
        max_tokens=200, temperature=0.2,
    )
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return 0, "unparseable"
    d = json.loads(m.group(0))
    return int(d.get("score", 0) or 0), str(d.get("reason", ""))[:80]


def _list(bank):
    for e in bank:
        print(f"{e.id:22s} [{e.tone:5s}] {e.quote[:46]} → {e.punchline[:46]}")


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="list bank entries and exit")
    ap.add_argument("--id", help="bank entry id (default: random)")
    ap.add_argument("--layout", choices=meme.LAYOUTS, help="force layout")
    ap.add_argument("--effect", choices=sorted(meme.EFFECTS), help="force effect")
    ap.add_argument("--punchline", help="override punchline (render-only, P6)")
    ap.add_argument("--seed", type=int, help="random seed for reproducible style")
    ap.add_argument("--glm", action="store_true",
                    help="P4 coherence check + P6 punchline variants via z.ai")
    ap.add_argument("--no-variants", action="store_true",
                    help="with --glm: skip the P6 variants call")
    ap.add_argument("--out", default=str(OUT_DIR), help="output directory")
    args = ap.parse_args()

    bank = meme.load_bank()
    if args.list:
        _list(bank)
        return

    model = os.getenv("MEME_MODEL", "glm-5.2")
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

    if args.glm:
        score, reason = _coherence({
            "quote": entry.quote, "punchline": entry.punchline,
            "tone": entry.tone, "bg_tag": bg_tag, "layout": style.layout,
            "img_source": img_source,
        }, model)
        print(f"P4 связность: {score}/5 — {reason}")
        if 0 < score < 3:
            print("⚠️ диссонанс: на сервере был бы ре-ролл; попробуй --layout")
        if not args.no_variants:
            for i, v in enumerate(_variants(entry, model), 1):
                print(f"  {i}. {v}")


if __name__ == "__main__":
    main()
