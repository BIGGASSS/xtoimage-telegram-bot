"""/video: provider MP4 selection, the live card slot, and real ffmpeg compositing."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image
from test_providers import client_for, fixture, x_payload
from test_render import png_asset, renderer  # noqa: F401  (pytest fixture)

from xtoimage_bot.media import MediaLoader
from xtoimage_bot.models import Box, Frame, Media, Post, RenderAssets, UserError
from xtoimage_bot.providers import FxTwitterProvider, XApiProvider
from xtoimage_bot.render import Renderer, build_html
from xtoimage_bot.video import (
    MAX_OUTPUT_HEIGHT,
    VideoComposer,
    count_videos,
    ffmpeg_arguments,
    find_live_media,
)

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")
CDN = "https://video.twimg.com/ext_tw_video/1/pu/vid/avc1"


def fx_video(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": "video",
        "url": f"{CDN}/1280x720/top.mp4",
        "thumbnail_url": "https://pbs.twimg.com/media/thumb.jpg",
        "width": 1280,
        "height": 720,
        "duration": 30.5,
        "formats": [
            {"container": "m3u8", "url": f"{CDN}/pl/list.m3u8"},
            {"container": "mp4", "codec": "h264", "bitrate": 256000, "url": f"{CDN}/a/low.mp4"},
            {"container": "mp4", "codec": "h264", "bitrate": 2176000, "url": f"{CDN}/b/mid.mp4"},
            {"container": "mp4", "codec": "h264", "bitrate": 10368000, "url": f"{CDN}/c/hi.mp4"},
            {"container": "mp4", "codec": "hevc", "bitrate": 1000000, "url": f"{CDN}/d/hevc.mp4"},
        ],
    }
    item.update(overrides)
    return item


async def fx_post(*media: dict[str, Any]) -> Post:
    payload = fixture()
    payload["status"]["media"] = {"all": list(media)}
    async with client_for(payload) as client:
        return await FxTwitterProvider(client).fetch("20")


async def test_fx_video_picks_best_h264_mp4_under_budget() -> None:
    post = await fx_post(fx_video())
    media = post.media[0]
    assert media.kind == "video"
    assert media.video_url == f"{CDN}/b/mid.mp4"
    assert media.duration == 30.5
    assert not media.animated


async def test_fx_long_video_picks_a_smaller_file() -> None:
    # 10 minutes at 2.2 Mbit/s would be ~160 MB; 256 kbit/s fits the download budget.
    post = await fx_post(fx_video(duration=600))
    assert post.media[0].video_url == f"{CDN}/a/low.mp4"


async def test_fx_gif_and_legacy_variants() -> None:
    gif = {
        "type": "gif",
        "thumbnail_url": "https://pbs.twimg.com/tweet_video_thumb/x.jpg",
        "url": "https://video.twimg.com/tweet_video/x.mp4",
    }
    legacy = fx_video(
        formats=None,
        url="https://video.twimg.com/top.m3u8",
        variants=[{"content_type": "video/mp4", "bitrate": 832000, "url": f"{CDN}/v.mp4"}],
    )
    post = await fx_post(gif, legacy)
    assert post.media[0].animated
    assert post.media[0].video_url == "https://video.twimg.com/tweet_video/x.mp4"
    assert post.media[1].video_url == f"{CDN}/v.mp4"


@pytest.mark.parametrize(
    "url",
    [
        "http://video.twimg.com/a.mp4",
        "https://video.twimg.com.evil.example/a.mp4",
        "https://user@video.twimg.com/a.mp4",
        "https://video.twimg.com:8443/a.mp4",
        "https://video.twimg.com/a.m3u8",
        "https://pbs.twimg.com/a.mp4",
        "https://video.twimg.com/a.mp4#x",
        "https://video.twimg.com/a b.mp4",
    ],
)
async def test_fx_unsafe_video_urls_are_dropped(url: str) -> None:
    post = await fx_post(fx_video(url=url, formats=[{"container": "mp4", "url": url}]))
    assert post.media[0].kind == "video"
    assert post.media[0].video_url == ""


async def test_fx_malformed_video_metadata_never_fails_the_post() -> None:
    post = await fx_post(fx_video(formats="nope", variants={"x": 1}, duration="long", url=None))
    assert post.media[0].video_url == ""
    assert post.media[0].duration is None


async def test_x_api_video_variants_and_gif() -> None:
    payload = x_payload()
    payload["includes"]["media"][0].update(
        {
            "duration_ms": 12000,
            "variants": [
                {"content_type": "application/x-mpegURL", "url": f"{CDN}/list.m3u8"},
                {"content_type": "video/mp4", "bit_rate": 832000, "url": f"{CDN}/a.mp4"},
                {"content_type": "video/mp4", "bit_rate": 2176000, "url": f"{CDN}/b.mp4"},
            ],
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert "variants" in request.url.params["media.fields"]
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        post = await XApiProvider(client, "local-test-token").fetch("20")
    assert post.media[0].video_url == f"{CDN}/b.mp4"
    assert post.media[0].duration == 12.0

    payload["includes"]["media"][0].update(
        type="animated_gif",
        variants=[{"content_type": "video/mp4", "bit_rate": 0, "url": f"{CDN}/g.mp4"}],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        post = await XApiProvider(client, "local-test-token").fetch("20")
    assert post.media[0].animated
    assert post.media[0].video_url == f"{CDN}/g.mp4"


def card_post(**changes: Any) -> Post:
    base = Post("1", "https://x.com/example/status/1", "Demo", "example", "Watch this")
    return replace(base, **changes)


def test_find_live_media_prefers_post_then_quote_then_parent() -> None:
    photo = Media("https://pbs.twimg.com/p.jpg")
    clip = Media("https://pbs.twimg.com/v.jpg", kind="video", video_url=f"{CDN}/v.mp4")
    assert find_live_media(card_post(media=(photo,))) is None
    quoted = card_post(media=(photo, clip))
    found = find_live_media(card_post(media=(photo,), quote=quoted, parent=quoted))
    assert found is not None and found.slot == ("quote", 1)
    found = find_live_media(card_post(media=(clip,), quote=quoted))
    assert found is not None and found.slot == ("post", 0)
    found = find_live_media(card_post(parent=quoted))
    assert found is not None and found.slot == ("parent", 1)
    assert count_videos(card_post(media=(clip,), quote=quoted)) == 2
    # A playable clip beats an earlier one without a downloadable MP4.
    stuck = replace(clip, video_url="")
    found = find_live_media(card_post(media=(stuck,), quote=quoted))
    assert found is not None and found.slot == ("quote", 1)
    found = find_live_media(card_post(media=(stuck,)))
    assert found is not None and found.slot == ("post", 0) and not found.media.video_url


def test_live_slot_has_no_play_badge_and_keeps_other_badges() -> None:
    clip = Media("", kind="video", alt_text='"><script>')
    post = card_post(media=(clip, clip))
    assets = RenderAssets(media=(png_asset(), png_asset()))
    document = build_html(post, assets, "light", ("post", 0))
    assert document.count('id="live"') == 1
    assert document.count("Video thumbnail</figcaption>") == 1
    assert "<script>" not in document
    # No thumbnail: an empty black box still gives the clip a place to play.
    document = build_html(post, RenderAssets(), "dark", ("post", 1))
    assert '<figure class="media live-empty" id="live"></figure>' in document
    # Without a live slot the card is unchanged.
    assert 'id="live"' not in build_html(post, assets)


def png_bytes(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_frame(height: int = 800, slot: Box | None = None) -> Frame:
    slot = slot or Box(100, 200, 640, 360)
    # The cover is transparent over the slot except a 16 px opaque white top-left corner.
    cover = Image.new("RGBA", (slot.width, slot.height), (255, 255, 255, 0))
    cover.paste((255, 255, 255, 255), (0, 0, 16, 16))
    card = png_bytes(Image.new("RGB", (1080, height), "white"))
    return Frame(card, 1080, height, slot, slot, png_bytes(cover))


def test_ffmpeg_arguments_are_locked_down() -> None:
    arguments = ffmpeg_arguments(
        make_frame(3001), "card.png", "cover.png", "in.mp4", "out.mp4", audio=False
    )
    assert arguments.count("-protocol_whitelist") == 3
    assert arguments[arguments.index("-i") - 2 : arguments.index("-i")] == ["-framerate", "30"]
    source = arguments.index("in.mp4")
    # The clip is read only as a local MP4 file, trimmed to 60 seconds.
    assert arguments[source - 7 : source] == [
        "-protocol_whitelist",
        "file",
        "-f",
        "mov",
        "-t",
        "60",
        "-i",
    ]
    assert "-an" in arguments
    graph = arguments[arguments.index("-filter_complex") + 1]
    # Tall cards are scaled to fit Telegram-friendly dimensions, with even sizes.
    assert f":{MAX_OUTPUT_HEIGHT}:flags" in graph
    assert "overlay=x=100+(640-w)/2:y=200+(360-h)/2" in graph
    assert "[2:v]overlay=x=100:y=200:eof_action=repeat" in graph


async def test_composer_without_ffmpeg_is_unavailable() -> None:
    composer = VideoComposer("")
    composer.executable = None
    assert not composer.available
    with pytest.raises(UserError, match="set up"):
        await composer.compose(make_frame(), b"")


def make_clip(path: Path, *, audio: bool, color: str = "red", seconds: float = 1.0) -> bytes:
    assert FFMPEG is not None
    command = [FFMPEG, "-nostdin", "-loglevel", "error", "-y"]
    command += ["-f", "lavfi", "-i", f"color=c={color}:s=320x180:r=25:d={seconds}"]
    if audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", str(path)]
    subprocess.run(command, check=True)
    return path.read_bytes()


def probe(data: bytes, folder: Path) -> tuple[Image.Image, str]:
    assert FFMPEG is not None
    video, still = folder / "result.mp4", folder / "frame.png"
    video.write_bytes(data)
    subprocess.run(
        [FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-ss", "0.5", "-i", str(video)]
        + ["-frames:v", "1", str(still)],
        check=True,
    )
    info = subprocess.run(
        [FFMPEG, "-nostdin", "-hide_banner", "-i", str(video)], capture_output=True, text=True
    ).stderr
    return Image.open(still).convert("RGB"), info


@needs_ffmpeg
async def test_composer_plays_clip_inside_slot_with_audio(tmp_path: Path) -> None:
    source = make_clip(tmp_path / "clip.mp4", audio=True)
    result = await VideoComposer(FFMPEG).compose(make_frame(), source, audio=True)
    assert (result.width, result.height) == (1080, 800)
    frame, info = probe(result.data, tmp_path)
    assert frame.size == (1080, 800)
    red, green, blue = frame.getpixel((100 + 320, 200 + 180))
    assert red > 200 and green < 60 and blue < 60  # the clip plays in the slot
    assert min(frame.getpixel((20, 20))) > 245
    assert min(frame.getpixel((104, 204))) > 230  # the cover's opaque corner stays
    assert "Audio: aac" in info and "Video: h264" in info
    assert "Duration: 00:00:01." in info  # ends with the clip, not at the 60 s cap


@needs_ffmpeg
async def test_composer_gif_mode_drops_audio_and_scales_tall_cards(tmp_path: Path) -> None:
    source = make_clip(tmp_path / "clip.mp4", audio=True, color="blue")
    frame = make_frame(2401, Box(0, 1000, 1080, 608))
    result = await VideoComposer(FFMPEG).compose(frame, source, audio=False)
    assert result.height == MAX_OUTPUT_HEIGHT and result.width % 2 == 0
    still, info = probe(result.data, tmp_path)
    assert still.size == (result.width, result.height)
    assert "Audio" not in info


@needs_ffmpeg
async def test_composer_rejects_non_video_input(tmp_path: Path) -> None:
    with pytest.raises(UserError, match="couldn’t make a video"):
        await VideoComposer(FFMPEG).compose(make_frame(), b"\x00\x00\x00\x18ftypmp42" + b"0" * 64)


async def test_video_download_is_allowlisted_and_checked() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        body = b"\x00\x00\x00\x18ftypmp42" if "good" in request.url.path else b"<html>"
        return httpx.Response(200, content=body + b"\x00" * 32)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        loader = MediaLoader(client)
        assert (await loader.load_video(f"{CDN}/good.mp4"))[4:8] == b"ftyp"
        with pytest.raises(UserError, match="could not be downloaded"):
            await loader.load_video(f"{CDN}/bad.mp4")
        with pytest.raises(UserError, match="could not be downloaded"):
            await loader.load_video("https://pbs.twimg.com/good.mp4")
    assert requests == [f"{CDN}/good.mp4", f"{CDN}/bad.mp4"]


@pytest.mark.browser
async def test_real_frame_measures_the_thumbnail_slot(renderer: Renderer) -> None:  # noqa: F811
    clip = Media("", kind="video")
    post = card_post(media=(Media(""), clip))
    assets = RenderAssets(media=(png_asset("#00ff00"), png_asset("#ff0000")))
    frame = await renderer.render_frame(post, assets, "light", ("post", 1))
    image = Image.open(BytesIO(frame.png)).convert("RGB")
    assert image.size == (frame.width, frame.height)
    slot = frame.slot
    assert slot.x > 540 and slot.width < 540 and slot.y > 0
    # The middle of the slot is the red thumbnail the clip will cover.
    assert image.getpixel((slot.x + slot.width // 2, slot.y + slot.height // 2)) == (255, 0, 0)
    # The cover is see-through over the slot, but opaque on the grid's rounded corner.
    cover = Image.open(BytesIO(frame.cover))
    assert cover.mode == "RGBA"
    assert cover.size == (frame.region.width, frame.region.height)
    region = frame.region
    assert region.x <= slot.x and region.x + region.width >= slot.x + slot.width
    assert cover.getpixel((region.width // 2, region.height // 2))[3] == 0
    assert cover.getpixel((region.width - 1, 0))[3] > 128
    # A missing thumbnail still gives the clip a measured box.
    empty = card_post(media=(clip,))
    frame = await renderer.render_frame(empty, RenderAssets(), "dark", ("post", 0))
    assert frame.slot.height == 540


@pytest.mark.browser
@pytest.mark.parametrize(("section", "height"), [("quote", 300), ("parent", 320)])
async def test_real_frame_without_preview_in_quote_or_parent(
    renderer: Renderer,  # noqa: F811
    section: str,
    height: int,
) -> None:
    # A single preview-less clip: .single section rules must not collapse the slot.
    inner = card_post(id="2", author_handle="other", text="Inner", media=(Media("", kind="video"),))
    post = card_post(**{section: inner})
    frame = await renderer.render_frame(post, RenderAssets(), "light", (section, 0))  # type: ignore[arg-type]
    assert frame.slot.height == height
    assert frame.slot.width > 300


@pytest.mark.browser
@needs_ffmpeg
async def test_end_to_end_card_video(renderer: Renderer, tmp_path: Path) -> None:  # noqa: F811
    clip = Media("", kind="video")
    post = card_post(media=(clip,))
    frame = await renderer.render_frame(
        post, RenderAssets(media=(png_asset("#00ff00"),)), "dark", ("post", 0)
    )
    source = make_clip(tmp_path / "clip.mp4", audio=False, color="red")
    result = await asyncio.wait_for(VideoComposer(FFMPEG).compose(frame, source), 60)
    still, _ = probe(result.data, tmp_path)
    slot = frame.slot
    red, green, _blue = still.getpixel((slot.x + slot.width // 2, slot.y + slot.height // 2))
    assert red > 200 and green < 60


async def test_fx_playable_video_without_preview_reaches_video_command() -> None:
    from test_bot import make_app, update

    post = await fx_post(fx_video(thumbnail_url=None))
    assert post.media[0].url == ""
    assert post.media[0].video_url == f"{CDN}/b/mid.mp4"
    assert any("no usable still preview" in warning for warning in post.warnings)
    # Without a preview or an MP4 the attachment is still dropped.
    assert (await fx_post(fx_video(thumbnail_url=None, formats=[], url=None))).media == ()

    app, transport, service, provider, mock_renderer = await make_app()
    provider.fetch.return_value = post
    async with app:
        await app.process_update(update(app, "/video https://x.com/jack/status/20"))
        service.media.load_video.assert_awaited_once_with(f"{CDN}/b/mid.mp4")
        assert mock_renderer.render_frame.call_args.args[3] == ("post", 0)
        assert any(name == "sendVideo" for name, _ in transport.calls)


async def test_x_api_playable_video_without_preview_reaches_video_command() -> None:
    from test_bot import make_app, update

    payload = x_payload()
    media = payload["includes"]["media"][0]
    del media["preview_image_url"]
    media["variants"] = [{"content_type": "video/mp4", "bit_rate": 832000, "url": f"{CDN}/a.mp4"}]
    async with client_for(payload) as client:
        post = await XApiProvider(client, "local-test-token").fetch("20")
    assert post.media[0].url == "" and post.media[0].video_url == f"{CDN}/a.mp4"

    app, transport, service, provider, _ = await make_app()
    provider.fetch.return_value = post
    async with app:
        await app.process_update(update(app, "/video https://x.com/jack/status/20"))
        service.media.load_video.assert_awaited_once_with(f"{CDN}/a.mp4")
        assert any(name == "sendVideo" for name, _ in transport.calls)
