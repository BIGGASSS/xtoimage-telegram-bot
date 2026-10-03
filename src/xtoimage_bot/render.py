"""Network-isolated HTML → PNG post cards.

Post strings are text, never markup. Media is accepted only as bounded, validated
PNG data URLs; neither post URLs nor upstream image URLs are opened by Chromium.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import html
import math
import os
import struct
from contextlib import suppress
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from .models import Post, RenderAssets, Theme, UserError

WIDTH = 1080
MAX_HEIGHT = 6000
MAX_TEXT_LENGTH = 12000
MAX_ASSET_BYTES = 6 * 1024 * 1024
MAX_ASSET_PIXELS = 16_000_000
RENDER_TIMEOUT_SECONDS = 35
_PNG_PREFIX = "data:image/png;base64,"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_CSS = """
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; width: 1080px; }
body { font-family: 'Inter', 'Arial', 'Noto Sans', 'Noto Color Emoji', sans-serif; }
#capture {
  width: 1080px; color: var(--ink);
  background: var(--paper);
  --ink: #18232d; --muted: #677481; --subtle: #84909a;
  --paper: #ffffff; --line: #e6ebee; --quote: #f8fafb; --media: #e8edf0;
}
#capture.dark {
  --ink: #f1f5f7; --muted: #a1b0bc; --subtle: #8396a6;
  --paper: #16232e; --line: #2c3b47; --quote: #1b2b38; --media: #0f1a22;
}
.card { padding: 44px; }
.author { display: flex; gap: 18px; align-items: center; min-width: 0; }
.avatar { width: 76px; height: 76px; flex: 0 0 76px; border-radius: 50%;
  object-fit: cover; background: var(--media); }
.avatar.placeholder { display: flex; align-items: center; justify-content: center;
  color: var(--muted); font-size: 28px; font-weight: 700; }
.identity { min-width: 0; flex: 1; }
.name { font-size: 29px; line-height: 1.3; font-weight: 750; overflow-wrap: anywhere; }
.handle { font-size: 22px; line-height: 1.5; color: var(--muted); overflow-wrap: anywhere; }
.platform { font-size: 34px; line-height: 1; padding-left: 16px; align-self: flex-start;
  color: var(--ink); font-weight: 500; }
.body { margin-top: 30px; font-size: 32px; line-height: 1.48; letter-spacing: -.3px;
  white-space: pre-wrap; overflow-wrap: anywhere; unicode-bidi: plaintext; }
.media-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px; margin-top: 28px; border-radius: 19px; overflow: hidden; }
.media-grid.single { grid-template-columns: minmax(0, 1fr); }
.media { margin: 0; position: relative; background: var(--media); min-width: 0;
  height: 310px; overflow: hidden; display: flex; align-items: center; justify-content: center; }
.media img { width: 100%; height: 100%; object-fit: contain; display: block; }
.single .media { height: auto; max-height: 570px; }
.single .media img { max-height: 570px; height: auto; }
.media-grid.three .media:first-child { grid-column: span 2; height: 350px; }
.media.unavailable, .single .media.unavailable { height: 240px; padding: 32px;
  font-size: 21px; line-height: 1.45; color: var(--muted); text-align: center; }
.video-label { position: absolute; bottom: 16px; left: 16px; border-radius: 7px;
  padding: 8px 12px; color: #fff; background: rgba(12, 21, 29, .83);
  font-size: 16px; line-height: 20px; letter-spacing: .3px; }
.quote { margin-top: 28px; padding: 26px; border: 1px solid var(--line);
  border-radius: 21px; background: var(--quote); }
.quote .avatar { width: 46px; height: 46px; flex-basis: 46px; font-size: 20px; }
.quote .author { gap: 12px; }
.quote .name { font-size: 22px; }
.quote .handle { font-size: 18px; }
.quote .body { margin-top: 18px; font-size: 25px; line-height: 1.48; letter-spacing: 0; }
.quote .media-grid { margin-top: 20px; gap: 8px; border-radius: 13px; }
.quote .media { height: 230px; }
.quote .single .media { height: auto; max-height: 390px; }
.quote .single img { max-height: 390px; }
.quote .three .media:first-child { height: 260px; }
.quote .unavailable, .quote .single .unavailable { height: 150px; font-size: 18px; }
.quote .source { margin-top: 20px; padding-top: 16px; font-size: 16px; }
.source { border-top: 1px solid var(--line); margin-top: 30px; padding-top: 24px;
  font-size: 18px; line-height: 1.6; color: var(--muted); overflow-wrap: anywhere; }
.timestamp { margin-bottom: 3px; }
.counts { display: flex; flex-wrap: wrap; gap: 12px 30px; margin-top: 20px;
  font-size: 18px; line-height: 1.5; color: var(--muted); }
.counts b { font-weight: 700; color: var(--ink); margin-right: 6px; }
"""


def _text(value: str) -> str:
    return html.escape(value, quote=True)


def _validate_asset(asset: str) -> str:
    """Defense in depth after the downloader's decode/re-encode boundary."""
    if not asset:
        return ""
    if not asset.startswith(_PNG_PREFIX) or len(asset) > MAX_ASSET_BYTES * 4 // 3 + 64:
        raise UserError("An image could not be safely rendered. Please try another post.")
    try:
        data = base64.b64decode(asset[len(_PNG_PREFIX) :], validate=True)
        if len(data) > MAX_ASSET_BYTES or len(data) < 33:
            raise ValueError("PNG length")
        if data[:8] != _PNG_SIGNATURE or data[8:16] != b"\x00\x00\x00\rIHDR":
            raise ValueError("PNG header")
        width, height = struct.unpack(">II", data[16:24])
        if width < 1 or height < 1 or width * height > MAX_ASSET_PIXELS:
            raise ValueError("PNG dimensions")
    except (binascii.Error, ValueError, struct.error) as exc:
        raise UserError("An image could not be safely rendered. Please try another post.") from exc
    return asset


def _handle(post: Post) -> str:
    return "@" + post.author_handle.lstrip("@")


def _avatar(name: str, asset: str) -> str:
    if asset:
        return f'<img class="avatar" src="{_validate_asset(asset)}" alt="">'
    initial = next((character for character in name.strip() if not character.isspace()), "?")
    return f'<span class="avatar placeholder" aria-hidden="true">{_text(initial)}</span>'


def _author(post: Post, avatar: str, *, quote: bool = False) -> str:
    return (
        '<header class="author">'
        + _avatar(post.author_name, avatar)
        + '<div class="identity">'
        + f'<div class="name" dir="auto">{_text(post.author_name)}</div>'
        + f'<div class="handle">{_text(_handle(post))}</div></div>'
        + ("" if quote else '<span class="platform" aria-label="X">𝕏</span>')
        + "</header>"
    )


def _media(post: Post, assets: tuple[str, ...]) -> str:
    media = post.media[:4]
    if not media:
        return ""
    count_class = "single" if len(media) == 1 else "three" if len(media) == 3 else ""
    output = [f'<div class="media-grid {count_class}">']
    for index, item in enumerate(media):
        asset = _validate_asset(assets[index]) if index < len(assets) else ""
        if not asset:
            label = "Video thumbnail unavailable" if item.kind == "video" else "Image unavailable"
            output.append(f'<figure class="media unavailable">{label}</figure>')
            continue
        alt = _text(item.alt_text)
        output.append(f'<figure class="media"><img src="{asset}" alt="{alt}">')
        if item.kind == "video":
            output.append('<figcaption class="video-label">▶ Video thumbnail</figcaption>')
        output.append("</figure>")
    output.append("</div>")
    return "".join(output)


def _timestamp(raw: str) -> str:
    if not raw:
        return ""
    try:
        try:
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            moment = parsedate_to_datetime(raw)
        if len(raw) == 10:
            return moment.strftime("%d %b %Y")
        if moment.tzinfo is not None:
            moment = moment.astimezone(UTC)
            return moment.strftime("%d %b %Y · %H:%M UTC")
        return moment.strftime("%d %b %Y · %H:%M")
    except (TypeError, ValueError, OverflowError):
        return raw


def _source(post: Post) -> str:
    timestamp = _timestamp(post.created_at)
    if not timestamp:
        return ""
    return f'<footer class="source"><div class="timestamp">{_text(timestamp)}</div></footer>'


def _counts(post: Post) -> str:
    counts = []
    for value, label in (
        (post.replies, "replies"),
        (post.reposts, "reposts"),
        (post.likes, "likes"),
    ):
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            counts.append(f"<span><b>{value:,}</b>{label}</span>")
    return f'<div class="counts">{"".join(counts)}</div>' if counts else ""


def build_html(post: Post, assets: RenderAssets, theme: Theme = "light") -> str:
    """Construct inert, standalone markup; exposed for security regression tests."""
    if theme not in ("light", "dark"):
        raise UserError("Choose the light or dark theme.")
    for item in (post, post.quote):
        if item is None:
            continue
        # Limit the total text handed to the layout engine, including metadata.
        if len(item.text) > MAX_TEXT_LENGTH or any(
            len(value) > 2048
            for value in (item.author_name, item.author_handle, item.created_at, item.url)
        ):
            raise UserError("This post is too long to fit in an image. Please try a shorter post.")
    # Reject invalid assets even when the matching media entry is absent.
    for asset in (assets.avatar, *assets.media, assets.quote_avatar, *assets.quote_media):
        _validate_asset(asset)
    quote = ""
    if post.quote is not None:
        quoted = post.quote
        quote = (
            '<section class="quote" aria-label="Quoted post">'
            + _author(quoted, assets.quote_avatar, quote=True)
            + (f'<div class="body" dir="auto">{_text(quoted.text)}</div>' if quoted.text else "")
            + _media(quoted, assets.quote_media)
            + _source(quoted)
            + "</section>"
        )
    csp = (
        "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; "
        "img-src data:; font-src 'none'; connect-src 'none'; frame-src 'none'; "
        "object-src 'none'; base-uri 'none'; form-action 'none'"
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        + f'<meta http-equiv="Content-Security-Policy" content="{csp}">'
        + '<meta name="color-scheme" content="light dark">'
        + f'<style>{_CSS}</style></head><body><main id="capture" class="{theme}">'
        + '<article class="card">'
        + _author(post, assets.avatar)
        + (f'<div class="body" dir="auto">{_text(post.text)}</div>' if post.text else "")
        + _media(post, assets.media)
        + quote
        + _source(post)
        + _counts(post)
        + "</article></main></body></html>"
    )


class Renderer:
    """One browser, bounded concurrent jobs, a disposable offline context per job.

    Call close() at application shutdown. A closed instance cannot be restarted.
    The owner should keep a bounded request queue in front of this class.
    """

    def __init__(self, executable_path: str | None = None, max_concurrency: int = 2) -> None:
        if not 1 <= max_concurrency <= 8:
            raise ValueError("max_concurrency must be between 1 and 8")
        self._executable_path = executable_path or os.getenv("CHROMIUM_EXECUTABLE_PATH") or None
        self._max_concurrency = max_concurrency
        self._slots = asyncio.Semaphore(max_concurrency)
        self._lifecycle_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._closed = False

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                raise UserError("The image renderer is shutting down. Please try again later.")
            if self._browser is not None and self._browser.is_connected():
                return
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None
            try:
                async with asyncio.timeout(20):
                    self._playwright = await async_playwright().start()
                    self._browser = await self._playwright.chromium.launch(
                        headless=True,
                        executable_path=self._executable_path,
                        timeout=15000,
                        args=["--disable-background-networking", "--disable-component-update"],
                    )
            except (PlaywrightError, TimeoutError, OSError) as exc:
                if self._playwright is not None:
                    with suppress(Exception):
                        await self._playwright.stop()
                self._playwright = None
                self._browser = None
                raise UserError(
                    "The image renderer is unavailable. Please try again later."
                ) from exc

    async def render(self, post: Post, assets: RenderAssets, theme: Theme = "light") -> bytes:
        document = build_html(post, assets, theme)
        async with self._slots:
            await self.start()
            context: BrowserContext | None = None
            try:
                async with asyncio.timeout(RENDER_TIMEOUT_SECONDS):
                    assert self._browser is not None
                    context = await self._browser.new_context(
                        viewport={"width": WIDTH, "height": 900},
                        device_scale_factor=1,
                        java_script_enabled=False,
                        service_workers="block",
                        offline=True,
                        accept_downloads=False,
                        locale="en-US",
                        timezone_id="UTC",
                        color_scheme=theme,
                    )
                    await context.route("**/*", lambda route: route.abort())
                    page = await context.new_page()
                    page.set_default_timeout(10000)
                    await page.set_content(document, wait_until="load", timeout=10000)
                    capture = page.locator("#capture")
                    bounds = await capture.bounding_box()
                    if bounds is None or not math.isfinite(bounds["height"]):
                        raise UserError("This post could not be rendered. Please try another post.")
                    if math.ceil(bounds["height"]) > MAX_HEIGHT:
                        raise UserError(
                            "This post is too tall to fit in an image. Please try a shorter post."
                        )
                    return await capture.screenshot(
                        type="png", animations="disabled", timeout=15000
                    )
            except (PlaywrightError, TimeoutError) as exc:
                raise UserError("This post could not be rendered. Please try again later.") from exc
            finally:
                if context is not None:
                    with suppress(Exception):
                        async with asyncio.timeout(5):
                            await context.close()

    async def close(self) -> None:
        # Serialize simultaneous shutdown requests so two callers cannot each
        # hold half the semaphore permits while waiting for the other half.
        async with self._close_lock:
            async with self._lifecycle_lock:
                self._closed = True
            # Drain in-flight renders before closing the shared browser. Renders
            # queued earlier observe _closed in start() and release their slot.
            acquired = 0
            try:
                for _ in range(self._max_concurrency):
                    await self._slots.acquire()
                    acquired += 1
                async with self._lifecycle_lock:
                    if self._browser is not None:
                        with suppress(Exception):
                            async with asyncio.timeout(5):
                                await self._browser.close()
                        self._browser = None
                    if self._playwright is not None:
                        with suppress(Exception):
                            async with asyncio.timeout(5):
                                await self._playwright.stop()
                        self._playwright = None
            finally:
                for _ in range(acquired):
                    self._slots.release()
