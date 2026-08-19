#!/usr/bin/env python3
"""Печать deck.html в PDF формата 16:9 (13.333 × 7.5 дюйма, как слайд 1920×1080).

Требуется Playwright с установленным Chromium:

    python -m pip install playwright
    python -m playwright install chromium

Запуск:

    python docs/presentation/build_pdf.py
    python docs/presentation/build_pdf.py --out /path/to/deck.pdf

Тот же результат даёт Ctrl+P в браузере: правила @page и @media print
в deck.html задают размер листа, светлую палитру и разрывы страниц.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DECK = HERE / "deck.html"

# 338.667 × 190.5 мм — те же 13.333 × 7.5 дюйма, что и в @page.
PAGE_WIDTH = "13.333in"
PAGE_HEIGHT = "7.5in"


async def render(source: Path, target: Path, executable_path: str | None) -> None:
    from playwright.async_api import async_playwright

    launch: dict = {"args": ["--no-sandbox"]}
    if executable_path:
        launch["executable_path"] = executable_path

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**launch)
        context = await browser.new_context(
            viewport={"width": 1600, "height": 900},
            color_scheme="light",
        )
        page = await context.new_page()
        await page.goto(source.as_uri())

        # Шрифты Google Fonts и разметку листов нужно дождаться до печати,
        # иначе заголовки уедут в системный fallback.
        await page.wait_for_load_state("networkidle")
        await page.evaluate("() => document.fonts.ready")
        await page.wait_for_timeout(600)

        await page.pdf(
            path=str(target),
            width=PAGE_WIDTH,
            height=PAGE_HEIGHT,
            print_background=True,
            prefer_css_page_size=True,
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
        )
        await browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, default=DECK, help="исходный deck.html")
    parser.add_argument("--out", type=Path, default=HERE / "deck.pdf", help="файл PDF")
    parser.add_argument(
        "--chromium",
        default=os.environ.get("CHROMIUM_PATH"),
        help="путь к бинарю Chromium, если Playwright не находит свой",
    )
    args = parser.parse_args()

    if not args.src.exists():
        print(f"Не найден {args.src}", file=sys.stderr)
        return 1

    asyncio.run(render(args.src, args.out, args.chromium))
    size = args.out.stat().st_size / 1024
    print(f"{args.out} — {size:.0f} КБ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
