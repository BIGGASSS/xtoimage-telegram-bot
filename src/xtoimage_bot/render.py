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
from io import BytesIO
from typing import Literal

from PIL import Image, ImageChops
from playwright.async_api import (
    Browser,
    BrowserContext,
    FloatRect,
    Locator,
    Playwright,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError

from .models import Box, Frame, Post, RenderAssets, Theme, UserError

WIDTH = 1080
MAX_HEIGHT = 6000
MAX_TEXT_LENGTH = 12000
MAX_ASSET_BYTES = 6 * 1024 * 1024
MAX_ASSET_PIXELS = 16_000_000
RENDER_TIMEOUT_SECONDS = 35
_PNG_PREFIX = "data:image/png;base64,"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PLACEHOLDER_TONES = 6

# Where /video plays its clip: which post on the card, and which of its media items.
LiveSection = Literal["post", "quote", "parent"]
LiveSlot = tuple[LiveSection, int]

# Static, trusted inline icons (Material Symbols-style paths). Never built from post data.
_ICON_PLAY = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.14v13.72L19 12z"/></svg>'
_ICONS = {
    "replies": (
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 2H4a2 2 0 0 0-2 2v18l4-4h14'
        'a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2zm0 14H5.17L4 17.17V4h16v12z"/></svg>'
    ),
    "reposts": (
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7h10v3l4-4-4-4v3H5v6h2V7z'
        'm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4z"/></svg>'
    ),
    "likes": (
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 '
        "12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 "
        '19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg>'
    ),
}

_CSS = """
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; width: 1080px; }
body { font-family: 'Inter', 'Segoe UI', 'Helvetica Neue', 'Noto Sans', 'Arial',
  'Noto Sans CJK JP', 'Noto Color Emoji', sans-serif;
  -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }
#capture {
  width: 1080px; color: var(--ink);
  background: var(--paper);
  --ink: #0f1419; --muted: #536471; --subtle: #8b98a5;
  --paper: #ffffff; --line: #e7ecf0; --quote: #ffffff; --media: #eff3f4;
  --like: #f91880;
}
#capture.dark {
  --ink: #e7e9ea; --muted: #8b98a5; --subtle: #6e7c89;
  --paper: #15202b; --line: #2f3d4b; --quote: #15202b; --media: #1e2c3a;
  --like: #f91880;
}
.card { padding: 56px 60px 52px; }
.author { display: flex; gap: 20px; align-items: center; min-width: 0; }
.avatar { width: 84px; height: 84px; flex: 0 0 84px; border-radius: 50%;
  object-fit: cover; background: var(--media);
  box-shadow: inset 0 0 0 1px rgba(0, 0, 0, .04); }
.avatar.placeholder { display: flex; align-items: center; justify-content: center;
  color: #fff; font-size: 34px; font-weight: 700; background: #1d9bf0; }
.avatar.tone1 { background: #7856ff; } .avatar.tone2 { background: #00ba7c; }
.avatar.tone3 { background: #ff7a00; } .avatar.tone4 { background: #f91880; }
.avatar.tone5 { background: #536471; }
.identity { min-width: 0; flex: 1; }
.name { font-size: 30px; line-height: 1.25; font-weight: 700; letter-spacing: -.2px;
  overflow-wrap: anywhere; }
.handle { margin-top: 2px; font-size: 24px; line-height: 1.35; color: var(--muted);
  overflow-wrap: anywhere; }
.platform { font-size: 38px; line-height: 1; padding-left: 16px; align-self: flex-start;
  color: var(--ink); font-weight: 500; }
.body { margin-top: 28px; font-size: 34px; line-height: 1.42; letter-spacing: -.2px;
  white-space: pre-wrap; overflow-wrap: anywhere; unicode-bidi: plaintext; }
.media-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px; margin-top: 30px; border-radius: 24px; overflow: hidden;
  border: 1px solid var(--line); background: var(--line); }
.media-grid.single { grid-template-columns: minmax(0, 1fr); }
.media { margin: 0; position: relative; background: var(--media); min-width: 0;
  height: 340px; overflow: hidden; display: flex; align-items: center; justify-content: center; }
.media img { width: 100%; height: 100%; object-fit: contain; display: block; }
.single .media { height: auto; max-height: 640px; }
.single .media img { max-height: 640px; height: auto; }
.media-grid.three .media:first-child { grid-column: span 2; height: 380px; }
.media.unavailable, .single .media.unavailable { height: 240px; padding: 32px;
  font-size: 21px; line-height: 1.45; color: var(--muted); text-align: center; }
.video-label { position: absolute; bottom: 18px; left: 18px; border-radius: 999px;
  display: flex; align-items: center; gap: 8px;
  padding: 7px 16px 7px 12px; color: #fff; background: rgba(0, 0, 0, .72);
  font-size: 17px; line-height: 22px; font-weight: 600; letter-spacing: .2px; }
.video-label svg { width: 18px; height: 18px; fill: currentColor; }
.media.live-empty, .single .media.live-empty { height: 540px; background: #000; }
.quote .media.live-empty { height: 300px; }
.parent .media.live-empty { height: 320px; }
.fill-black #live { filter: brightness(0); }
.fill-white #live { filter: brightness(0) invert(1); }
.play { position: absolute; top: 50%; left: 50%; width: 88px; height: 88px;
  margin: -44px 0 0 -44px; border-radius: 50%; background: rgba(15, 20, 25, .72);
  border: 3px solid rgba(255, 255, 255, .9); display: flex; align-items: center;
  justify-content: center; }
.play svg { width: 38px; height: 38px; margin-left: 5px; fill: #fff; }
.quote { margin-top: 30px; padding: 24px 26px 26px; border: 1px solid var(--line);
  border-radius: 24px; background: var(--quote); }
.quote .author { gap: 10px; align-items: baseline; flex-wrap: wrap; }
.quote .avatar { width: 36px; height: 36px; flex-basis: 36px; font-size: 17px;
  align-self: center; }
.quote .identity { display: flex; flex-wrap: wrap; align-items: baseline;
  gap: 0 10px; min-width: 0; }
.quote .name { font-size: 24px; }
.quote .handle, .quote .when { font-size: 22px; margin-top: 0; color: var(--muted); }
.quote .when::before { content: "·"; margin-right: 10px; }
.quote .body { margin-top: 14px; font-size: 27px; line-height: 1.42; letter-spacing: 0; }
.quote .media-grid { margin-top: 18px; gap: 3px; border-radius: 16px; }
.quote .media { height: 240px; }
.quote .single .media { height: auto; max-height: 420px; }
.quote .single img { max-height: 420px; }
.quote .three .media:first-child { height: 270px; }
.quote .unavailable, .quote .single .unavailable { height: 150px; font-size: 18px; }
.parent { display: grid; grid-template-columns: 64px minmax(0, 1fr); column-gap: 20px;
  margin-bottom: 8px; }
.parent .rail { display: flex; flex-direction: column; align-items: center; }
.parent .avatar { width: 64px; height: 64px; flex: 0 0 64px; font-size: 26px; }
.parent .thread-line { flex: 1; width: 3px; min-height: 24px; margin-top: 8px;
  border-radius: 2px; background: var(--line); }
.parent .content { min-width: 0; padding-bottom: 32px; }
.parent .identity { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 10px; }
.parent .name { font-size: 26px; }
.parent .handle, .parent .when { font-size: 23px; margin-top: 0; color: var(--muted); }
.parent .when::before { content: "·"; margin-right: 10px; }
.parent .body { margin-top: 8px; font-size: 29px; line-height: 1.42; letter-spacing: 0; }
.parent .media-grid { margin-top: 20px; gap: 3px; border-radius: 18px; }
.parent .media { height: 260px; }
.parent .single .media { height: auto; max-height: 460px; }
.parent .single img { max-height: 460px; }
.parent .three .media:first-child { height: 290px; }
.parent .unavailable, .parent .single .unavailable { height: 160px; font-size: 18px; }
.replying-to { margin-top: 14px; font-size: 23px; line-height: 1.35; color: var(--muted);
  overflow-wrap: anywhere; }
.source { margin-top: 32px; font-size: 22px; line-height: 1.5; color: var(--muted);
  overflow-wrap: anywhere; }
.counts { display: flex; flex-wrap: wrap; gap: 14px 40px; margin-top: 22px;
  padding-top: 22px; border-top: 1px solid var(--line);
  font-size: 22px; line-height: 1.4; color: var(--muted); }
.counts > span { display: inline-flex; align-items: center; }
.counts svg { width: 24px; height: 24px; margin-right: 10px; fill: var(--muted); }
.counts .likes svg { fill: var(--like); }
.counts b { font-weight: 700; color: var(--ink); margin-right: 7px;
  font-variant-numeric: tabular-nums; }
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


def _avatar(name: str, asset: str, seed: str = "") -> str:
    if asset:
        return f'<img class="avatar" src="{_validate_asset(asset)}" alt="">'
    initial = next((character for character in name.strip() if not character.isspace()), "?")
    # Deterministic, data-independent class name: only a small integer reaches the markup.
    tone = sum((seed or name).encode("utf-8")) % _PLACEHOLDER_TONES
    return (
        f'<span class="avatar placeholder tone{tone}" aria-hidden="true">'
        f"{_text(initial.upper())}</span>"
    )


def _author(post: Post, avatar: str, *, quote: bool = False) -> str:
    when = _timestamp(post.created_at) if quote else ""
    return (
        '<header class="author">'
        + _avatar(post.author_name, avatar, post.author_handle)
        + '<div class="identity">'
        + f'<div class="name" dir="auto">{_text(post.author_name)}</div>'
        + f'<div class="handle">{_text(_handle(post))}</div>'
        + (f'<div class="when">{_text(when)}</div>' if when else "")
        + "</div>"
        + ("" if quote else '<span class="platform" aria-label="X">𝕏</span>')
        + "</header>"
    )


def _parent(post: Post, avatar: str, media: tuple[str, ...], live: int | None = None) -> str:
    """The replied-to post, above the reply, joined to it by a conversation line."""
    when = _timestamp(post.created_at)
    return (
        '<section class="parent" aria-label="Replied-to post">'
        + '<div class="rail">'
        + _avatar(post.author_name, avatar, post.author_handle)
        + '<span class="thread-line" aria-hidden="true"></span></div>'
        + '<div class="content"><div class="identity">'
        + f'<div class="name" dir="auto">{_text(post.author_name)}</div>'
        + f'<div class="handle">{_text(_handle(post))}</div>'
        + (f'<div class="when">{_text(when)}</div>' if when else "")
        + "</div>"
        + (f'<div class="body" dir="auto">{_text(post.text)}</div>' if post.text else "")
        + _media(post, media, live)
        + "</div></section>"
    )


def _media(post: Post, assets: tuple[str, ...], live: int | None = None) -> str:
    """Media grid. The `live` item is where /video overlays the clip: no play badge."""
    media = post.media[:4]
    if not media:
        return ""
    count_class = "single" if len(media) == 1 else "three" if len(media) == 3 else ""
    output = [f'<div class="media-grid {count_class}">']
    for index, item in enumerate(media):
        asset = _validate_asset(assets[index]) if index < len(assets) else ""
        if index == live:
            image = f'<img src="{asset}" alt="">' if asset else ""
            empty = "" if asset else " live-empty"
            output.append(f'<figure class="media{empty}" id="live">{image}</figure>')
            continue
        if not asset:
            label = "Video thumbnail unavailable" if item.kind == "video" else "Image unavailable"
            output.append(f'<figure class="media unavailable">{label}</figure>')
            continue
        alt = _text(item.alt_text)
        output.append(f'<figure class="media"><img src="{asset}" alt="{alt}">')
        if item.kind == "video":
            output.append(f'<span class="play" aria-hidden="true">{_ICON_PLAY}</span>')
            output.append('<figcaption class="video-label">Video thumbnail</figcaption>')
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
            noun = {"replies": "reply", "reposts": "repost", "likes": "like"}[label]
            noun = noun if value == 1 else label
            counts.append(f'<span class="{label}">{_ICONS[label]}<b>{value:,}</b>{noun}</span>')
    return f'<div class="counts">{"".join(counts)}</div>' if counts else ""


def build_html(
    post: Post,
    assets: RenderAssets,
    theme: Theme = "light",
    live: LiveSlot | None = None,
    fill: Literal["", "black", "white"] = "",
) -> str:
    """Construct inert, standalone markup; exposed for security regression tests."""
    if theme not in ("light", "dark"):
        raise UserError("Choose the light or dark theme.")
    section, index = live if live is not None else ("post", -1)
    # Mask passes for /video paint the live slot one flat colour; see Renderer.render_frame.
    capture_class = f"{theme} fill-{fill}" if fill in ("black", "white") else theme
    for item in (post, post.quote, post.parent):
        if item is None:
            continue
        # Limit the total text handed to the layout engine, including metadata.
        if len(item.text) > MAX_TEXT_LENGTH or any(
            len(value) > 2048
            for value in (item.author_name, item.author_handle, item.created_at, item.url)
        ):
            raise UserError("This post is too long to fit in an image. Please try a shorter post.")
    # Reject invalid assets even when the matching media entry is absent.
    for asset in (
        assets.avatar,
        *assets.media,
        assets.quote_avatar,
        *assets.quote_media,
        assets.parent_avatar,
        *assets.parent_media,
    ):
        _validate_asset(asset)
    parent = ""
    replying_to = ""
    if post.parent is not None:
        parent = _parent(
            post.parent,
            assets.parent_avatar,
            assets.parent_media,
            index if section == "parent" else None,
        )
        replying_to = f'<div class="replying-to">Replying to {_text(_handle(post.parent))}</div>'
    quote = ""
    if post.quote is not None:
        quoted = post.quote
        quote = (
            '<section class="quote" aria-label="Quoted post">'
            + _author(quoted, assets.quote_avatar, quote=True)
            + (f'<div class="body" dir="auto">{_text(quoted.text)}</div>' if quoted.text else "")
            + _media(quoted, assets.quote_media, index if section == "quote" else None)
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
        + f'<style>{_CSS}</style></head><body><main id="capture" class="{capture_class}">'
        + '<article class="card">'
        + parent
        + _author(post, assets.avatar)
        + replying_to
        + (f'<div class="body" dir="auto">{_text(post.text)}</div>' if post.text else "")
        + _media(post, assets.media, index if section == "post" else None)
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
        shots, _ = await self._capture((build_html(post, assets, theme),), theme)
        return shots[0]

    async def render_frame(
        self, post: Post, assets: RenderAssets, theme: Theme, live: LiveSlot
    ) -> Frame:
        """The card for /video, the box the clip plays in, and a cover for the clip.

        Two extra passes paint the slot black, then white: their difference is exactly
        what the viewer sees of the slot, which becomes the cover's transparent hole.
        """
        documents = (
            build_html(post, assets, theme, live),
            build_html(post, assets, theme, live, "black"),
            build_html(post, assets, theme, live, "white"),
        )
        (png, black, white), slot = await self._capture(documents, theme, measure=True)
        assert slot is not None
        region, cover = await asyncio.to_thread(_slot_cover, png, black, white)
        width, height = struct.unpack(">II", png[16:24])
        return Frame(png=png, width=width, height=height, slot=slot, region=region, cover=cover)

    async def _capture(
        self, documents: tuple[str, ...], theme: Theme, *, measure: bool = False
    ) -> tuple[list[bytes], Box | None]:
        """Screenshot each document in one offline page; optionally measure #live in the first."""
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
                    shots: list[bytes] = []
                    slot: Box | None = None
                    for document in documents:
                        await page.set_content(document, wait_until="load", timeout=10000)
                        capture = page.locator("#capture")
                        bounds = await capture.bounding_box()
                        if bounds is None or not math.isfinite(bounds["height"]):
                            raise UserError(
                                "This post could not be rendered. Please try another post."
                            )
                        if math.ceil(bounds["height"]) > MAX_HEIGHT:
                            raise UserError(
                                "This post is too tall to fit in an image. "
                                "Please try a shorter post."
                            )
                        if measure and slot is None:
                            slot = await self._measure(page.locator("#live"), bounds)
                        shots.append(
                            await capture.screenshot(
                                type="png", animations="disabled", timeout=15000
                            )
                        )
                    return shots, slot
            except (PlaywrightError, TimeoutError) as exc:
                raise UserError("This post could not be rendered. Please try again later.") from exc
            finally:
                if context is not None:
                    with suppress(Exception):
                        async with asyncio.timeout(5):
                            await context.close()

    @staticmethod
    async def _measure(live: Locator, card: FloatRect) -> Box:
        """The clip's box on the card. The thumbnail <img> has the clip's aspect ratio."""
        image = live.locator("img")
        box = await (image if await image.count() else live).bounding_box()
        if box is None or not all(
            map(math.isfinite, (box["x"], box["y"], box["width"], box["height"]))
        ):
            raise UserError("This post could not be rendered. Please try another post.")
        width, height = round(card["width"]), round(card["height"])
        left = max(0, round(box["x"] - card["x"]))
        top = max(0, round(box["y"] - card["y"]))
        slot = Box(
            x=left,
            y=top,
            width=min(round(box["width"]), width - left),
            height=min(round(box["height"]), height - top),
        )
        if slot.width < 16 or slot.height < 16:
            raise UserError("This post could not be rendered. Please try another post.")
        return slot

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


def _slot_cover(card: bytes, black: bytes, white: bytes) -> tuple[Box, bytes]:
    """The card around the visible slot, with the slot itself transparent."""
    with (
        Image.open(BytesIO(card)) as base,
        Image.open(BytesIO(black)) as dark,
        Image.open(BytesIO(white)) as light,
    ):
        if not base.size == dark.size == light.size:
            raise UserError("This post could not be rendered. Please try again later.")
        visible = ImageChops.difference(dark.convert("L"), light.convert("L"))
        bounds = visible.getbbox()
        if bounds is None:
            raise UserError("This post could not be rendered. Please try again later.")
        cover = base.convert("RGB").crop(bounds)
    cover.putalpha(ImageChops.invert(visible.crop(bounds)))
    output = BytesIO()
    cover.save(output, format="PNG")
    left, top, right, bottom = bounds
    return Box(left, top, right - left, bottom - top), output.getvalue()
