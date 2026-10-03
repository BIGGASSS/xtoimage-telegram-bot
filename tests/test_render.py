from __future__ import annotations

import asyncio
import base64
import os
import shutil
from collections.abc import AsyncIterator
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from playwright.async_api import async_playwright

from xtoimage_bot.models import Media, Post, RenderAssets, UserError
from xtoimage_bot.render import MAX_HEIGHT, WIDTH, Renderer, build_html


@pytest.fixture
def post() -> Post:
    return Post(
        id="1",
        url="https://x.com/example/status/1",
        author_name="Demo Author",
        author_handle="example",
        text="First line.\n\nA second line. こんにちは 🌱",
        created_at="2026-10-02T19:00:00Z",
        likes=0,
    )


def png_asset(color: str = "#b5d9b2") -> str:
    buffer = BytesIO()
    Image.new("RGB", (120, 100), color).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def test_untrusted_text_is_escaped_everywhere(post: Post) -> None:
    attack = '<script>alert(1)</script><img src="https://evil.test/" onerror="alert(2)">'
    document = build_html(
        replace(
            post,
            author_name=attack,
            author_handle=attack,
            text=attack,
            url=attack,
            created_at=attack,
            media=(Media("https://evil.test/", alt_text=attack),),
        ),
        RenderAssets(media=(png_asset(),)),
    )
    assert "<script>" not in document
    assert '<img src="https://evil.test/' not in document
    assert "&lt;script&gt;" in document
    assert "&quot;alert(2)&quot;" in document
    assert "default-src 'none'" in document
    assert "script-src 'none'" in document
    assert "img-src data:" in document
    assert "<a " not in document


@pytest.mark.parametrize(
    "asset",
    [
        "https://example.com/a.png",
        "file:///etc/passwd",
        "data:image/svg+xml,<svg/>",
        'data:image/png;base64," onerror="alert(1)',
        "data:image/png;base64,bm90IGEgcG5n",
        "data:image/png;base64,!!!!!",
        "data:image/PNG;base64,AAAA",
    ],
)
def test_rejects_unsafe_or_invalid_assets(post: Post, asset: str) -> None:
    with pytest.raises(UserError, match="safely rendered"):
        build_html(post, RenderAssets(avatar=asset))


def test_rejects_invalid_unused_assets(post: Post) -> None:
    with pytest.raises(UserError, match="safely rendered"):
        build_html(post, RenderAssets(quote_media=("https://evil.test/a.png",)))


def test_rejects_excessive_pixel_count(post: Post) -> None:
    raw = base64.b64decode(png_asset().split(",", 1)[1])
    raw = raw[:16] + (100_000).to_bytes(4, "big") + (100_000).to_bytes(4, "big") + raw[24:]
    bad = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    with pytest.raises(UserError, match="safely rendered"):
        build_html(post, RenderAssets(avatar=bad))


def test_only_available_counts_and_one_quote(post: Post) -> None:
    nested = replace(post, text="DO NOT RENDER NESTED QUOTE")
    quote = replace(post, author_handle="quote_author", text="Quoted words", quote=nested)
    document = build_html(replace(post, quote=quote, reposts=1200), RenderAssets())
    assert "DO NOT RENDER NESTED QUOTE" not in document
    assert document.count('aria-label="Quoted post"') == 1
    assert "@quote_author" in document
    assert "Quoted words" in document
    assert "<b>0</b>likes" in document
    assert "<b>1,200</b>reposts" in document
    assert "</b>replies" not in document
    assert "verified" not in document.lower()
    assert "02 Oct 2026 · 19:00 UTC" in document


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_single_clean_card_without_injected_source_urls(post: Post, theme: str) -> None:
    quote = replace(post, url="https://x.com/quote/status/2", text="Quoted words")
    document = build_html(replace(post, quote=quote), RenderAssets(), theme)  # type: ignore[arg-type]
    assert document.count('<article class="card">') == 1
    assert document.count('aria-label="Quoted post"') == 1
    assert "x.com" not in document
    for removed in ("masthead", "footnote", "source-url", "POST CARD", "FROM X", "linear-gradient"):
        assert removed not in document
    assert document.count("02 Oct 2026 · 19:00 UTC") == 2
    assert "@example" in document


def test_missing_timestamps_leave_no_empty_source_footer(post: Post) -> None:
    undated = replace(post, created_at="")
    document = build_html(replace(undated, quote=undated), RenderAssets())
    assert '<footer class="source">' not in document


def test_authored_links_are_preserved_as_post_content(post: Post) -> None:
    text = "Read https://x.com/example/status/42 and https://example.com/?a=1&b=2"
    document = build_html(replace(post, text=text, quote=replace(post, text=text)), RenderAssets())
    assert document.count("https://x.com/example/status/42") == 2
    assert document.count("https://example.com/?a=1&amp;b=2") == 2
    assert post.url not in document


def test_media_slots_remain_aligned_and_limit_four(post: Post) -> None:
    media = (Media("a"), Media("b", kind="video"), Media("c"), Media("d"), Media("e"))
    document = build_html(replace(post, media=media), RenderAssets(media=("", png_asset())))
    assert document.count('<figure class="media') == 4
    assert document.count("Image unavailable") == 3
    assert "Video thumbnail" in document
    assert "Video thumbnail unavailable" not in document


def test_rejects_long_text_and_unknown_theme(post: Post) -> None:
    with pytest.raises(UserError, match="too long"):
        build_html(replace(post, text="a" * 12001), RenderAssets())
    with pytest.raises(UserError, match="theme"):
        build_html(post, RenderAssets(), 'evil"><script>')  # type: ignore[arg-type]


def test_renderer_rejects_invalid_concurrency() -> None:
    with pytest.raises(ValueError, match="max_concurrency"):
        Renderer(max_concurrency=0)


@pytest.fixture
async def renderer() -> AsyncIterator[Renderer]:
    executable = os.getenv("CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    executable = executable or shutil.which("chromium-browser")
    if not executable:
        async with async_playwright() as playwright:
            if not await asyncio.to_thread(Path(playwright.chromium.executable_path).exists):
                pytest.skip("Chromium is not installed; install it or set CHROMIUM_EXECUTABLE_PATH")
    instance = Renderer(executable_path=executable)
    await instance.start()
    try:
        yield instance
    finally:
        await instance.close()


@pytest.mark.browser
async def test_real_screenshot_light_dark_and_media(renderer: Renderer, post: Post) -> None:
    post = replace(post, media=(Media("", alt_text="Test art"),))
    results = await asyncio.gather(
        renderer.render(post, RenderAssets(avatar=png_asset(), media=(png_asset(),)), "light"),
        renderer.render(post, RenderAssets(media=(png_asset(),)), "dark"),
    )
    for result in results:
        assert result.startswith(b"\x89PNG\r\n\x1a\n")
        image = Image.open(BytesIO(result))
        assert image.width == WIDTH
        assert 500 < image.height <= MAX_HEIGHT
    light = Image.open(BytesIO(results[0])).convert("RGB")
    dark = Image.open(BytesIO(results[1])).convert("RGB")
    # The card itself fills the canvas: no gradient, margin, shadow, or outer frame.
    for image, paper in ((light, (255, 255, 255)), (dark, (22, 35, 46))):
        for point in ((0, 0), (WIDTH - 1, 0), (0, image.height - 1), (WIDTH - 1, image.height - 1)):
            assert image.getpixel(point) == paper
        assert image.getpixel((20, image.height // 2)) == paper


@pytest.mark.browser
async def test_tall_posts_are_rejected_not_clipped(renderer: Renderer, post: Post) -> None:
    with pytest.raises(UserError, match="too tall"):
        await renderer.render(replace(post, text="A short line\n" * 240), RenderAssets())


@pytest.mark.browser
async def test_browser_cannot_execute_post_content_or_fetch_urls(
    renderer: Renderer,
    post: Post,
) -> None:
    # Neither the text URL nor Media.url nor avatar_url is browser input.
    hostile = replace(
        post,
        text='<img src="http://127.0.0.1:9/" onerror="document.body.remove()">',
        avatar_url="http://127.0.0.1:9/",
        media=(Media("file:///etc/passwd"),),
    )
    screenshot = await renderer.render(hostile, RenderAssets())
    assert Image.open(BytesIO(screenshot)).width == WIDTH


@pytest.mark.browser
async def test_close_is_idempotent_and_prevents_new_jobs(renderer: Renderer, post: Post) -> None:
    await renderer.close()
    await renderer.close()
    with pytest.raises(UserError, match="shutting down"):
        await renderer.render(post, RenderAssets())


def fake_browser_renderer() -> tuple[Renderer, object, object, object]:
    from unittest.mock import AsyncMock, MagicMock

    from playwright.async_api import Browser, BrowserContext, Page

    capture = MagicMock()
    capture.bounding_box = AsyncMock(return_value={"x": 0, "y": 0, "width": WIDTH, "height": 600})
    capture.screenshot = AsyncMock(return_value=b"mock PNG bytes")
    page = MagicMock(spec=Page)
    page.set_content = AsyncMock()
    page.locator.return_value = capture
    context = MagicMock(spec=BrowserContext)
    context.route = AsyncMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()
    browser = MagicMock(spec=Browser)
    browser.is_connected.return_value = True
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()
    renderer = Renderer()
    renderer._browser = browser
    return renderer, browser, context, capture


async def test_context_disables_network_scripts_and_closes_after_render(post: Post) -> None:
    from unittest.mock import AsyncMock

    renderer, browser, context, _ = fake_browser_renderer()
    result = await renderer.render(post, RenderAssets())
    assert result == b"mock PNG bytes"
    options = browser.new_context.call_args.kwargs
    assert options["java_script_enabled"] is False
    assert options["offline"] is True
    assert options["service_workers"] == "block"
    assert options["accept_downloads"] is False
    pattern, callback = context.route.call_args.args
    assert pattern == "**/*"
    route = AsyncMock()
    await callback(route)
    route.abort.assert_awaited_once()
    context.close.assert_awaited_once()
    await renderer.close()


async def test_render_timeout_returns_safe_error_and_closes_context(post: Post) -> None:
    renderer, _, context, capture = fake_browser_renderer()
    capture.screenshot.side_effect = TimeoutError("secret upstream URL must not reach user")
    with pytest.raises(UserError, match="could not be rendered") as error:
        await renderer.render(post, RenderAssets())
    assert "secret" not in str(error.value)
    context.close.assert_awaited_once()
    await renderer.close()


async def test_concurrent_close_drains_inflight_render_without_deadlock(post: Post) -> None:
    renderer, browser, _, capture = fake_browser_renderer()
    screenshot_started = asyncio.Event()
    release_screenshot = asyncio.Event()

    async def screenshot(**kwargs: object) -> bytes:
        screenshot_started.set()
        await release_screenshot.wait()
        return b"PNG"

    capture.screenshot.side_effect = screenshot
    render_task = asyncio.create_task(renderer.render(post, RenderAssets()))
    await screenshot_started.wait()
    close_tasks = [asyncio.create_task(renderer.close()), asyncio.create_task(renderer.close())]
    await asyncio.sleep(0)
    browser.close.assert_not_awaited()
    release_screenshot.set()
    async with asyncio.timeout(2):
        await asyncio.gather(render_task, *close_tasks)
    browser.close.assert_awaited_once()
    with pytest.raises(UserError, match="shutting down"):
        await renderer.render(post, RenderAssets())
