import json
from unittest.mock import AsyncMock

import pytest
from telegram import Update
from telegram.ext import Application
from telegram.request import BaseRequest

from xtoimage_bot.bot import Admission, BotService
from xtoimage_bot.config import Config
from xtoimage_bot.models import Box, Frame, Media, Post, RenderAssets, UserError
from xtoimage_bot.video import Video


class TelegramTransport(BaseRequest):
    def __init__(self):
        self.calls = []

    @property
    def read_timeout(self):
        return 5.0

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((endpoint, params))
        if endpoint == "getMe":
            result = {
                "id": 123456,
                "is_bot": True,
                "first_name": "Post Card",
                "username": "postcard_bot",
            }
        elif endpoint == "sendChatAction":
            result = True
        else:
            result = {
                "message_id": len(self.calls),
                "date": 1,
                "chat": {"id": params.get("chat_id", 77), "type": "private"},
                "text": params.get("text", ""),
            }
        return 200, json.dumps({"ok": True, "result": result}).encode()


def update(app, text, user_id=77, chat_type="private"):
    return Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1,
                "chat": {"id": user_id, "type": chat_type},
                "from": {"id": user_id, "first_name": "Test", "is_bot": False},
                "text": text,
                "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
                if text.startswith("/")
                else [],
            },
        },
        app.bot,
    )


async def make_app(**config_overrides):
    transport = TelegramTransport()
    app = (
        Application.builder()
        .token("123456:TEST_TOKEN_NOT_A_REAL_SECRET")
        .request(transport)
        .get_updates_request(transport)
        .build()
    )
    provider = AsyncMock()
    provider.fetch.return_value = Post(
        "20", "https://x.com/jack/status/20", "Jack", "jack", "Hello"
    )
    media = AsyncMock()
    media.load.return_value = RenderAssets()
    media.load_video.return_value = b"MP4-test-only"
    renderer = AsyncMock()
    renderer.render.return_value = b"PNG-test-only"
    renderer.render_frame.return_value = Frame(
        b"PNG", 1080, 800, Box(60, 200, 960, 540), Box(60, 200, 960, 540), b"cover"
    )
    composer = AsyncMock()
    composer.available = True
    composer.compose.return_value = Video(b"MP4-out", 1080, 800)
    service = BotService(
        Config("test:token", **config_overrides), provider, media, renderer, composer
    )
    service.register(app)
    await app.initialize()
    return app, transport, service, provider, renderer


async def test_real_telegram_update_pipeline_photo_and_theme():
    app, transport, service, provider, renderer = await make_app()
    async with app:
        await app.process_update(update(app, "/theme dark"))
        await app.process_update(update(app, "https://x.com/jack/status/20"))
        provider.fetch.assert_awaited_once_with("20")
        assert renderer.render.call_args.args[2] == "dark"
        photo = next(p for name, p in transport.calls if name == "sendPhoto")
        assert "https://x.com/jack/status/20" in photo["caption"]
        assert "parse_mode" not in photo
        assert not service.admission.active


async def test_file_command_delivers_original():
    app, transport, _, _, _ = await make_app()
    async with app:
        await app.process_update(update(app, "/file https://twitter.com/jack/status/20"))
        assert any(name == "sendDocument" for name, _ in transport.calls)
        assert not any(name == "sendPhoto" for name, _ in transport.calls)


async def test_allowlist_and_private_chat_reject_before_fetch():
    app, transport, _, provider, _ = await make_app(allowed_user_ids=frozenset({1}))
    async with app:
        await app.process_update(update(app, "https://x.com/jack/status/20"))
        await app.process_update(
            update(app, "https://x.com/jack/status/20", user_id=1, chat_type="group")
        )
        provider.fetch.assert_not_awaited()
        await app.process_update(update(app, "/id"))
        assert any(p.get("text") == "Your Telegram user ID: 77" for _, p in transport.calls)


async def test_provider_error_clears_admission_and_safe_reply():
    app, transport, service, provider, _ = await make_app()
    provider.fetch.side_effect = UserError("This post is unavailable.")
    async with app:
        await app.process_update(update(app, "https://x.com/jack/status/20"))
        assert not service.admission.active
        assert any(p.get("text") == "This post is unavailable." for _, p in transport.calls)


async def test_invalid_link_never_fetches():
    app, _, _, provider, _ = await make_app()
    async with app:
        await app.process_update(update(app, "https://127.0.0.1/secret"))
        provider.fetch.assert_not_awaited()


def test_admission_rate_concurrency_and_release():
    admission = Admission(1, 10)
    admission.enter(1)
    with pytest.raises(UserError, match="last image"):
        admission.enter(1)
    with pytest.raises(UserError, match="other images"):
        admission.enter(2)
    admission.leave(1)
    with pytest.raises(UserError, match="10 seconds"):
        admission.enter(1)
    admission.enter(2)


async def test_quote_warnings_are_delivered():
    from dataclasses import replace

    app, transport, _, provider, _ = await make_app()
    quote = Post(
        "21",
        "https://x.com/jack/status/21",
        "Jack",
        "jack",
        "Quoted",
        warnings=("A further nested quoted post is not shown.",),
    )
    provider.fetch.return_value = replace(provider.fetch.return_value, quote=quote)
    async with app:
        await app.process_update(update(app, "https://x.com/jack/status/20"))
        photo = next(p for name, p in transport.calls if name == "sendPhoto")
        assert "Quoted post: A further nested quoted post is not shown." in photo["caption"]


async def test_parent_warnings_are_delivered():
    from dataclasses import replace

    app, transport, _, provider, _ = await make_app()
    parent = Post(
        "19",
        "https://x.com/someone/status/19",
        "Someone",
        "someone",
        "Parent",
        warnings=("Earlier posts in this conversation are not shown.",),
    )
    provider.fetch.return_value = replace(provider.fetch.return_value, parent=parent)
    async with app:
        await app.process_update(update(app, "https://x.com/jack/status/20"))
        photo = next(p for name, p in transport.calls if name == "sendPhoto")
        assert (
            "Replied-to post: Earlier posts in this conversation are not shown." in photo["caption"]
        )


CLIP = Media(
    "https://pbs.twimg.com/thumb.jpg",
    kind="video",
    video_url="https://video.twimg.com/v.mp4",
    duration=95.0,
)


async def test_video_command_sends_card_video():
    from dataclasses import replace

    app, transport, service, provider, renderer = await make_app()
    provider.fetch.return_value = replace(
        provider.fetch.return_value,
        media=(CLIP,),
        quote=Post("21", "https://x.com/a/status/21", "A", "a", "Q", media=(CLIP,)),
        warnings=("Videos and animated GIFs are shown as still previews.",),
    )
    async with app:
        await app.process_update(update(app, "/video https://x.com/jack/status/20"))
        assert renderer.render_frame.call_args.args[3] == ("post", 0)
        service.media.load_video.assert_awaited_once_with("https://video.twimg.com/v.mp4")
        assert service.composer.compose.call_args.kwargs == {"audio": True}
        video = next(p for name, p in transport.calls if name == "sendVideo")
        assert video["supports_streaming"] is True
        assert (video["width"], video["height"]) == (1080, 800)
        assert "first 60 seconds" in video["caption"]
        assert "Only one video or GIF plays" in video["caption"]
        assert "still previews." not in video["caption"].split("plays;")[0]
        assert not any(name == "sendPhoto" for name, _ in transport.calls)
        actions = [p["action"] for name, p in transport.calls if name == "sendChatAction"]
        assert actions == ["upload_video"]
        assert not service.admission.active


async def test_video_command_sends_gif_as_silent_animation():
    from dataclasses import replace

    app, transport, service, provider, _ = await make_app()
    gif = replace(CLIP, animated=True, duration=None)
    provider.fetch.return_value = replace(provider.fetch.return_value, media=(gif,))
    async with app:
        await app.process_update(update(app, "/video https://x.com/jack/status/20"))
        assert service.composer.compose.call_args.kwargs == {"audio": False}
        animation = next(p for name, p in transport.calls if name == "sendAnimation")
        assert "seconds" not in animation["caption"]


async def test_video_command_without_video_explains():
    app, transport, service, _, renderer = await make_app()
    async with app:
        await app.process_update(update(app, "/video https://x.com/jack/status/20"))
        renderer.render_frame.assert_not_awaited()
        assert any("has no video or GIF" in p.get("text", "") for _, p in transport.calls)
        assert not service.admission.active


async def test_video_command_without_ffmpeg_never_fetches():
    app, transport, service, provider, _ = await make_app()
    service.composer.available = False
    async with app:
        await app.process_update(update(app, "/video https://x.com/jack/status/20"))
        provider.fetch.assert_not_awaited()
        assert any("isn’t set up" in p.get("text", "") for _, p in transport.calls)
