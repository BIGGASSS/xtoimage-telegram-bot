"""Download allowlisted images and re-encode them before browser rendering."""

import asyncio
import base64
from io import BytesIO

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from .models import Post, RenderAssets, UserError
from .network import MEDIA_HOSTS, VIDEO_HOSTS, fetch_bytes

MAX_PIXELS = 16_000_000
MAX_BYTES = 8_000_000
MAX_VIDEO_BYTES = 40_000_000
_VIDEO_FAILED = "The video could not be downloaded. Please try again later."
_VIDEO_TOO_LARGE = "This video is too large to download. Try /image instead."


def sanitize_image(raw: bytes) -> str:
    try:
        with Image.open(BytesIO(raw)) as source:
            if source.width * source.height > MAX_PIXELS or source.format not in {
                "JPEG",
                "PNG",
                "WEBP",
                "GIF",
            }:
                raise UserError("An attached image is too large or unsupported.")
            source.seek(0)
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            output = BytesIO()
            image.save(output, format="PNG", optimize=True)
            return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise UserError("An attached image could not be decoded safely.") from None


class MediaLoader:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self._slots = asyncio.Semaphore(4)

    async def load_video(self, url: str) -> bytes:
        """Download one MP4 for /video. ffmpeg decodes it later with its MP4 demuxer only."""
        try:
            raw = await fetch_bytes(
                self.client, url, hosts=VIDEO_HOSTS, limit=MAX_VIDEO_BYTES, time_limit=60
            )
        except UserError as error:
            large = "too large" in str(error)
            raise UserError(_VIDEO_TOO_LARGE if large else _VIDEO_FAILED) from None
        # Every MP4 starts with an `ftyp` box.
        if len(raw) < 12 or raw[4:8] != b"ftyp":
            raise UserError(_VIDEO_FAILED)
        return raw

    async def load(self, post: Post) -> RenderAssets:
        warnings: list[str] = []

        async def one(url: str, *, attachment: bool = False) -> str:
            if not url:
                if attachment:
                    warnings.append("Some media could not be loaded; placeholders are shown.")
                return ""
            try:
                async with self._slots:
                    raw = await fetch_bytes(self.client, url, hosts=MEDIA_HOSTS, limit=MAX_BYTES)
                    return await asyncio.to_thread(sanitize_image, raw)
            except UserError:
                if attachment:
                    warnings.append("Some media could not be loaded; placeholders are shown.")
                return ""

        avatar = await one(post.avatar_url)
        media = tuple(await asyncio.gather(*(one(m.url, attachment=True) for m in post.media)))
        quote_avatar = ""
        quote_media: tuple[str, ...] = ()
        if post.quote:
            quote_avatar = await one(post.quote.avatar_url)
            quote_media = tuple(
                await asyncio.gather(*(one(m.url, attachment=True) for m in post.quote.media))
            )
        parent_avatar = ""
        parent_media: tuple[str, ...] = ()
        if post.parent:
            parent_avatar = await one(post.parent.avatar_url)
            parent_media = tuple(
                await asyncio.gather(*(one(m.url, attachment=True) for m in post.parent.media))
            )
        return RenderAssets(
            avatar=avatar,
            media=media,
            quote_avatar=quote_avatar,
            quote_media=quote_media,
            parent_avatar=parent_avatar,
            parent_media=parent_media,
            warnings=tuple(dict.fromkeys(warnings)),
        )
