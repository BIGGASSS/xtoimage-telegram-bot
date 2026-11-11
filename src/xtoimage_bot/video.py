"""/video: play a post's clip inside its rendered card, composited by a local ffmpeg.

ffmpeg only ever reads two local temporary files: the PNG card we rendered and an MP4
downloaded from X's video CDN. The MP4 demuxer is forced (no format probing, so no HLS
playlists or other protocols) and only the `file` protocol is allowed.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .models import Frame, Media, Post, UserError
from .render import LiveSection, LiveSlot

MAX_SECONDS = 60
MAX_OUTPUT_HEIGHT = 1920
MAX_OUTPUT_BYTES = 48_000_000
FFMPEG_TIMEOUT_SECONDS = 150
FPS = 30
_FAILED = "I couldn’t make a video from this post. Try again, or send the link alone for an image."


@dataclass(frozen=True)
class LiveMedia:
    slot: LiveSlot
    media: Media


@dataclass(frozen=True)
class Video:
    data: bytes
    width: int
    height: int


def find_live_media(post: Post) -> LiveMedia | None:
    """The first video or GIF: in the post itself, then its quoted post, then its parent.

    One with a downloadable MP4 wins; otherwise the first one is returned so the caller
    can explain that it can't be played.
    """
    sections: tuple[tuple[LiveSection, Post | None], ...] = (
        ("post", post),
        ("quote", post.quote),
        ("parent", post.parent),
    )
    found = [
        LiveMedia((section, index), media)
        for section, item in sections
        if item is not None
        for index, media in enumerate(item.media[:4])
        if media.kind == "video"
    ]
    return next((live for live in found if live.media.video_url), found[0] if found else None)


def count_videos(post: Post) -> int:
    return sum(
        1
        for item in (post, post.quote, post.parent)
        if item is not None
        for media in item.media[:4]
        if media.kind == "video"
    )


def _even(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def ffmpeg_arguments(
    frame: Frame, card: str, cover: str, source: str, output: str, *, audio: bool
) -> list[str]:
    """Build the ffmpeg command line. Every value in it is a number we computed.

    The clip is fitted into the slot on the card, then the cover (the card around the
    slot, with the slot transparent) goes on top so the card's rounded corners stay.
    """
    slot, region = frame.slot, frame.region
    scale = min(1.0, MAX_OUTPUT_HEIGHT / frame.height)
    width, height = _even(frame.width * scale), _even(frame.height * scale)
    graph = (
        f"[1:v]fps={FPS},scale=w={slot.width}:h={slot.height}"
        ":force_original_aspect_ratio=decrease,setsar=1[clip];"
        f"[0:v][clip]overlay=x={slot.x}+({slot.width}-w)/2:y={slot.y}+({slot.height}-h)/2"
        ":shortest=1:eof_action=endall[playing];"
        # The cover is a single frame; overlay keeps showing it for the whole clip.
        f"[playing][2:v]overlay=x={region.x}:y={region.y}:eof_action=repeat,"
        f"scale={width}:{height}:flags=lanczos,setsar=1,format=yuv420p[out]"
    )
    arguments = [
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        # Input 0: the rendered card, repeated for the length of the clip.
        "-protocol_whitelist",
        "file",
        "-f",
        "image2",
        "-loop",
        "1",
        "-framerate",
        str(FPS),
        "-i",
        card,
        # Input 1: the downloaded clip, trimmed to MAX_SECONDS.
        "-protocol_whitelist",
        "file",
        "-f",
        "mov",
        "-t",
        str(MAX_SECONDS),
        "-i",
        source,
        # Input 2: the cover, one frame.
        "-protocol_whitelist",
        "file",
        "-f",
        "image2",
        "-i",
        cover,
        "-filter_complex",
        graph,
        "-map",
        "[out]",
    ]
    if audio:
        arguments += ["-map", "1:a:0?", "-c:a", "aac", "-b:a", "128k", "-ac", "2"]
    else:
        arguments += ["-an"]
    arguments += [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        # Caps the size: 60 s at 4 Mbit/s plus audio stays well under Telegram's 50 MB.
        "-maxrate",
        "4M",
        "-bufsize",
        "8M",
        "-r",
        str(FPS),
        "-t",
        str(MAX_SECONDS),
        "-threads",
        "2",
        "-movflags",
        "+faststart",
        "-map_metadata",
        "-1",
        "-f",
        "mp4",
        output,
    ]
    return arguments


class VideoComposer:
    """Runs one ffmpeg job at a time; temporary files are deleted after each job."""

    def __init__(self, executable_path: str | None = None) -> None:
        configured = executable_path or os.getenv("FFMPEG_EXECUTABLE_PATH") or None
        self.executable = configured or shutil.which("ffmpeg")
        self._slots = asyncio.Semaphore(1)

    @property
    def available(self) -> bool:
        return bool(self.executable)

    async def compose(self, frame: Frame, source: bytes, *, audio: bool = True) -> Video:
        if not self.executable:
            raise UserError("Video rendering isn’t set up on this bot.")
        async with self._slots:
            with tempfile.TemporaryDirectory(prefix="xtoimage-") as directory:
                folder = Path(directory)
                card, cover = folder / "card.png", folder / "cover.png"
                clip, output = folder / "in.mp4", folder / "out.mp4"
                card.write_bytes(frame.png)
                cover.write_bytes(frame.cover)
                clip.write_bytes(source)
                arguments = ffmpeg_arguments(
                    frame, str(card), str(cover), str(clip), str(output), audio=audio
                )
                await self._run(arguments)
                try:
                    size = output.stat().st_size
                except OSError:
                    raise UserError(_FAILED) from None
                if size == 0:
                    raise UserError(_FAILED)
                if size > MAX_OUTPUT_BYTES:
                    raise UserError("This video is too large to send. Try /image instead.")
                data = output.read_bytes()
        scale = min(1.0, MAX_OUTPUT_HEIGHT / frame.height)
        return Video(data, _even(frame.width * scale), _even(frame.height * scale))

    async def _run(self, arguments: list[str]) -> None:
        assert self.executable is not None
        process = await asyncio.create_subprocess_exec(
            self.executable,
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            # Never logged: ffmpeg errors can echo file names and stream metadata.
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(FFMPEG_TIMEOUT_SECONDS):
                code = await process.wait()
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise UserError("This video took too long to prepare. Please try again.") from None
        except asyncio.CancelledError:
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(Exception):
                await asyncio.shield(process.wait())
            raise
        if code != 0:
            raise UserError(_FAILED)
