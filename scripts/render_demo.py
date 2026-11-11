#!/usr/bin/env python3
"""Render deterministic synthetic fixtures without any upstream network access.

Run: CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium uv run python scripts/render_demo.py
All names, posts, counts, and illustrations below are fictional test data.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from xtoimage_bot.models import Media, Post, RenderAssets, Theme
from xtoimage_bot.render import Renderer, build_html


def _png(image: Image.Image) -> str:
    output = BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def fixture_art() -> tuple[str, str, str, str]:
    """In-memory test artwork, never fetched from a third-party service."""
    avatar = Image.new("RGB", (192, 192), "#f0d6bc")
    draw = ImageDraw.Draw(avatar)
    draw.ellipse((30, 82, 162, 242), fill="#315951")
    draw.ellipse((51, 32, 141, 124), fill="#7e4a37")
    draw.ellipse((59, 51, 133, 129), fill="#bd8465")
    draw.pieslice((46, 22, 145, 105), 170, 360, fill="#302b2a")
    draw.line((77, 84, 83, 84), fill="#302b2a", width=4)
    draw.line((109, 84, 115, 84), fill="#302b2a", width=4)
    draw.arc((87, 96, 108, 111), 10, 160, fill="#7e4a37", width=3)

    first = Image.new("RGB", (900, 660), "#d8e4d6")
    draw = ImageDraw.Draw(first)
    draw.rectangle((44, 42, 856, 618), outline="#9bab9a", width=2)
    draw.text((86, 75), "THE SLOW STUDIO", fill="#264a3d", font=_font(27))
    draw.text((85, 126), "Room to grow.", fill="#264a3d", font=_font(63))
    draw.ellipse((365, 236, 735, 606), fill="#355749")
    draw.ellipse((213, 278, 465, 530), fill="#c68b67")
    draw.rectangle((341, 430, 624, 619), fill="#b6c8ae")
    draw.rectangle((540, 375, 787, 460), fill="#f4efe0")
    draw.text((88, 557), "01 / SYNTHETIC TEST ART", fill="#264a3d", font=_font(20))

    second = Image.new("RGB", (900, 660), "#ede2d3")
    draw = ImageDraw.Draw(second)
    draw.text((82, 75), "A LITTLE EVERY DAY", fill="#68535e", font=_font(27))
    draw.rounded_rectangle((267, 163, 746, 620), radius=100, fill="#b7afc9")
    draw.ellipse((461, 251, 799, 589), fill="#665975")
    draw.rectangle((171, 387, 621, 505), fill="#d89976")
    draw.ellipse((99, 243, 328, 472), fill="#f6f1e7")
    draw.text((83, 557), "02 / SYNTHETIC TEST ART", fill="#68535e", font=_font(20))

    quote_avatar = Image.new("RGB", (120, 120), "#253d50")
    draw = ImageDraw.Draw(quote_avatar)
    draw.rounded_rectangle((31, 24, 89, 96), radius=5, outline="#d8e4d6", width=5)
    draw.line((44, 43, 77, 43), fill="#d8e4d6", width=4)
    draw.line((44, 57, 77, 57), fill="#d8e4d6", width=4)
    return _png(avatar), _png(first), _png(second), _png(quote_avatar)


def image_fixture() -> tuple[Post, RenderAssets]:
    avatar, first, second, quote_avatar = fixture_art()
    quote = Post(
        id="2",
        url="https://x.com/demo_notes/status/2",
        author_name="Studio Notes · demo",
        author_handle="demo_notes",
        text="Make a little room for the things you want to make.",
        created_at="2026-09-30T15:30:00Z",
    )
    post = Post(
        id="1",
        url="https://x.com/demo_author/status/1",
        author_name="Maya Chen · demo",
        author_handle="demo_author",
        text=(
            "A tiny weekend project, finally out in the world.\n\n"
            "A place for slower ideas, brighter colors, and a little room to grow. 🌱"
        ),
        created_at="2026-10-02T18:24:00Z",
        media=(
            Media("", alt_text="Green and terracotta abstract test artwork"),
            Media("", kind="video", alt_text="Violet and coral abstract test artwork"),
        ),
        quote=quote,
        likes=1284,
        reposts=86,
        replies=24,
    )
    return post, RenderAssets(avatar=avatar, media=(first, second), quote_avatar=quote_avatar)


def text_fixture() -> tuple[Post, RenderAssets]:
    post = Post(
        id="3",
        url="https://x.com/demo_notes/status/3",
        author_name="Studio Notes · demo",
        author_handle="demo_notes",
        text=(
            "Good ideas need a little breathing room.\n\n"
            "Write it down. Take a walk. Come back with fresh eyes.\n\n"
            "Small steps count.\n小さな一歩。 Un paso a la vez. خطوة بخطوة"
        ),
        created_at="2026-10-02T19:08:00Z",
        likes=42,
        replies=0,
    )
    return post, RenderAssets(avatar=fixture_art()[3])


def reply_fixture() -> tuple[Post, RenderAssets]:
    avatar, first, _, quote_avatar = fixture_art()
    parent = Post(
        id="4",
        url="https://x.com/demo_notes/status/4",
        author_name="Studio Notes · demo",
        author_handle="demo_notes",
        text="What is one small thing you made this week?",
        created_at="2026-10-02T17:40:00Z",
    )
    post = Post(
        id="5",
        url="https://x.com/demo_author/status/5",
        author_name="Maya Chen · demo",
        author_handle="demo_author",
        text="A little poster for the studio wall. 🌱",
        created_at="2026-10-02T18:02:00Z",
        media=(Media("", alt_text="Green and terracotta abstract test artwork"),),
        parent=parent,
        likes=318,
        reposts=12,
        replies=5,
    )
    return post, RenderAssets(avatar=avatar, media=(first,), parent_avatar=quote_avatar)


async def main(output: Path, html_only: bool = False) -> None:
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True)
    renderer = Renderer()
    try:
        for theme in ("light", "dark"):
            selected_theme: Theme = "light" if theme == "light" else "dark"
            for name, fixture in (
                ("image-rich", image_fixture),
                ("text-only", text_fixture),
                ("reply", reply_fixture),
            ):
                post, assets = fixture()
                html_path = output / f"{name}-{theme}.html"
                await asyncio.to_thread(
                    html_path.write_text, build_html(post, assets, selected_theme), encoding="utf-8"
                )
                if html_only:
                    print(html_path)
                    continue
                png = await renderer.render(post, assets, selected_theme)
                path = output / f"{name}-{theme}.png"
                await asyncio.to_thread(path.write_bytes, png)
                with Image.open(BytesIO(png)) as image:
                    print(f"{path}: {image.width}×{image.height}, {len(png):,} bytes")
    finally:
        await renderer.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("docs/examples"))
    parser.add_argument("--html-only", action="store_true", help="Write offline HTML previews only")
    arguments = parser.parse_args()
    asyncio.run(main(arguments.output, arguments.html_only))
