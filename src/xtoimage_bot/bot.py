"""Telegram handlers. Provider, downloader and renderer remain independently testable."""

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import replace
from io import BytesIO
from typing import Literal, Protocol

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction, ChatType
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .config import Config
from .models import Frame, Post, RenderAssets, Theme, UserError
from .providers import STILL_PREVIEWS
from .render import LiveSlot
from .urls import extract_post_link
from .video import MAX_SECONDS, Video, count_videos, find_live_media

LOG = logging.getLogger(__name__)
MAX_USERS = 10_000
HELP = (
    "Send me one public x.com or twitter.com post link and I’ll turn it into an image.\n\n"
    "/image <link> — shareable photo\n"
    "/file <link> — original PNG, without Telegram photo compression\n"
    "/video <link> — for a post with a video or GIF: the card with the clip playing in it\n"
    "/theme light or /theme dark — choose your style\n"
    "/id — show your numeric Telegram user ID\n"
    "/privacy — how post links and images are handled\n\n"
    "Photos and video thumbnails are supported, with one quoted post. Replies include the "
    "post they reply to. Private/deleted posts cannot be fetched. Videos are still images "
    f"except with /video, which plays the first {MAX_SECONDS} seconds of one clip. Longer "
    "threads and polls are not fully captured. Very long posts may not fit in one image."
)


class Provider(Protocol):
    async def fetch(self, post_id: str) -> Post: ...


class AssetLoader(Protocol):
    async def load(self, post: Post) -> RenderAssets: ...

    async def load_video(self, url: str) -> bytes: ...


class Renderer(Protocol):
    async def render(self, post: Post, assets: RenderAssets, theme: Theme = "light") -> bytes: ...

    async def render_frame(
        self, post: Post, assets: RenderAssets, theme: Theme, live: LiveSlot
    ) -> Frame: ...


class Composer(Protocol):
    @property
    def available(self) -> bool: ...

    async def compose(self, frame: Frame, source: bytes, *, audio: bool = True) -> Video: ...


Mode = Literal["photo", "document", "video"]


class Admission:
    """No waiting queue: one active request per user, a bounded global pool and cooldown."""

    def __init__(self, limit: int, cooldown: int) -> None:
        self.limit = limit
        self.cooldown = cooldown
        self.active: set[int] = set()
        self.last: OrderedDict[int, float] = OrderedDict()

    def enter(self, user_id: int) -> None:
        now = time.monotonic()
        if user_id in self.active:
            raise UserError("I’m still working on your last image. Please wait a moment.")
        if now - self.last.get(user_id, -float("inf")) < self.cooldown:
            raise UserError(f"Please wait {self.cooldown} seconds between images.")
        if len(self.active) >= self.limit:
            raise UserError("I’m rendering other images right now. Please try again shortly.")
        self.active.add(user_id)
        self.last[user_id] = now
        self.last.move_to_end(user_id)
        if len(self.last) > MAX_USERS:
            self.last.popitem(last=False)

    def leave(self, user_id: int) -> None:
        self.active.discard(user_id)


class BotService:
    def __init__(
        self,
        config: Config,
        provider: Provider,
        media: AssetLoader,
        renderer: Renderer,
        composer: Composer | None = None,
    ) -> None:
        self.config, self.provider, self.media, self.renderer = config, provider, media, renderer
        self.composer = composer
        self.admission = Admission(config.max_concurrency, config.user_cooldown_seconds)
        self.themes: OrderedDict[int, Theme] = OrderedDict()

    async def permitted(self, update: Update, *, allow_id: bool = False) -> bool:
        user, chat, message = update.effective_user, update.effective_chat, update.effective_message
        if user is None or chat is None or message is None or user.is_bot:
            return False
        if self.config.private_chats_only and chat.type != ChatType.PRIVATE:
            return False
        if (
            not allow_id
            and self.config.allowed_user_ids
            and user.id not in self.config.allowed_user_ids
        ):
            await message.reply_text("This is a private bot. Your account is not on its allowlist.")
            return False
        return True

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.permitted(update) and update.effective_message:
            provider = (
                "FxTwitter (third party)"
                if self.config.provider == "fxtwitter"
                else "the official X API"
            )
            await update.effective_message.reply_text(
                HELP + f"\n\nPost IDs are sent to {provider}. See /privacy for details."
            )

    async def identity(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if (
            await self.permitted(update, allow_id=True)
            and update.effective_message
            and update.effective_user
        ):
            await update.effective_message.reply_text(
                f"Your Telegram user ID: {update.effective_user.id}"
            )

    async def privacy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.permitted(update) or not update.effective_message:
            return
        provider = (
            "FxTwitter, an independent third-party service"
            if self.config.provider == "fxtwitter"
            else "the official X API"
        )
        await update.effective_message.reply_text(
            f"Post IDs are sent to {provider}; image requests go to X’s image CDN. "
            "For /video, the clip is downloaded from X’s video CDN and combined with the image "
            "in temporary files that are deleted right after. "
            "Rendered images, videos and messages go through Telegram. "
            "Only public posts are supported. This bot has no public gallery and does not save "
            "post content or images to disk. It keeps bounded cooldown/theme state in memory "
            "until restart. Service providers have their own retention policies. "
            "Only share content you have permission to reuse."
        )

    async def theme(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if (
            not await self.permitted(update)
            or not update.effective_message
            or not update.effective_user
        ):
            return
        args = context.args or []
        if len(args) != 1 or args[0].lower() not in {"light", "dark"}:
            await update.effective_message.reply_text("Choose /theme light or /theme dark.")
            return
        self.themes[update.effective_user.id] = "dark" if args[0].lower() == "dark" else "light"
        self.themes.move_to_end(update.effective_user.id)
        if len(self.themes) > MAX_USERS:
            self.themes.popitem(last=False)
        await update.effective_message.reply_text(
            f"{args[0].capitalize()} theme selected until the bot restarts."
        )

    async def image(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.convert(update, context, mode="photo")

    async def file(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.convert(update, context, mode="document")

    async def video(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.convert(update, context, mode="video")

    async def convert(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, *, mode: Mode
    ) -> None:
        if not await self.permitted(update):
            return
        message, user = update.effective_message, update.effective_user
        if message is None or user is None:
            return
        admitted = False
        try:
            # Only the literal message text is used; hidden text-link targets are not followed.
            link = extract_post_link(message.text or "")
            if mode == "video" and (self.composer is None or not self.composer.available):
                raise UserError("Video rendering isn’t set up on this bot. Try /image instead.")
            self.admission.enter(user.id)
            admitted = True
            # A video needs a download and an encode on top of the image.
            async with asyncio.timeout(240 if mode == "video" else 90):
                action = ChatAction.UPLOAD_VIDEO if mode == "video" else ChatAction.UPLOAD_PHOTO
                await context.bot.send_chat_action(message.chat_id, action)
                post = await self.provider.fetch(link.id)
                theme = self.themes.get(user.id, self.config.default_theme)
                markup = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Open original post", url=post.url)]]
                )
                if mode == "video":
                    await self._send_video(message, post, theme, markup)
                else:
                    document = mode == "document"
                    await self._send_image(message, post, theme, markup, document=document)
                LOG.info("render_delivered")
        except UserError as error:
            await message.reply_text(str(error))
        except TimeoutError:
            kind = "video" if mode == "video" else "image"
            await message.reply_text(
                f"That {kind} took too long to prepare. Please try again shortly."
            )
        except TelegramError:
            # Never print Telegram exceptions: request URLs can contain the bot token.
            LOG.warning("telegram_delivery_failed")
        except Exception as error:
            LOG.error("render_failed kind=%s", type(error).__name__)
            await message.reply_text("I couldn’t prepare this image. Please try again later.")
        finally:
            if admitted:
                self.admission.leave(user.id)

    @staticmethod
    def _caption(post: Post, assets: RenderAssets, extra: tuple[str, ...] = ()) -> str:
        quote_warnings = (
            tuple(f"Quoted post: {warning}" for warning in post.quote.warnings)
            if post.quote
            else ()
        )
        parent_warnings = (
            tuple(f"Replied-to post: {warning}" for warning in post.parent.warnings)
            if post.parent
            else ()
        )
        warnings = tuple(
            dict.fromkeys(
                (*post.warnings, *parent_warnings, *quote_warnings, *assets.warnings, *extra)
            )
        )
        caption = f"@{post.author_handle}\n{post.url}"
        if warnings:
            caption += "\n\n" + "\n".join(warnings)
        # Telegram photo/file captions are limited to 1024 characters.
        if len(caption) > 1000:
            caption = caption[:997] + "…"
        return caption

    async def _send_image(
        self,
        message: Message,
        post: Post,
        theme: Theme,
        markup: InlineKeyboardMarkup,
        *,
        document: bool,
    ) -> None:
        assets = await self.media.load(post)
        png = await self.renderer.render(post, assets, theme)
        if len(png) > 45_000_000:
            raise UserError("This image is too large to send safely.")
        caption = self._caption(post, assets)
        buffer = BytesIO(png)
        buffer.name = f"x-{post.id}-{theme}.png"
        if document or len(png) > 9_500_000:
            await message.reply_document(
                buffer,
                filename=buffer.name,
                caption=caption,
                reply_markup=markup,
                write_timeout=45,
            )
            return
        try:
            await message.reply_photo(
                buffer, caption=caption, reply_markup=markup, write_timeout=45
            )
        except BadRequest as error:
            # Telegram may reject a technically valid but unusually shaped PNG.
            reason = str(error).lower()
            if not any(
                term in reason
                for term in ("photo_invalid_dimensions", "photo_invalid", "image_process_failed")
            ):
                raise
            buffer.seek(0)
            await message.reply_document(
                buffer,
                filename=buffer.name,
                caption=caption,
                reply_markup=markup,
                write_timeout=45,
            )

    async def _send_video(
        self, message: Message, post: Post, theme: Theme, markup: InlineKeyboardMarkup
    ) -> None:
        live = find_live_media(post)
        if live is None:
            raise UserError(
                "This post has no video or GIF. Send the link without /video to get an image."
            )
        if not live.media.video_url:
            raise UserError("This post’s video can’t be downloaded. Try /image instead.")
        assets = await self.media.load(post)
        source = await self.media.load_video(live.media.video_url)
        frame = await self.renderer.render_frame(post, assets, theme, live.slot)
        assert self.composer is not None
        result = await self.composer.compose(frame, source, audio=not live.media.animated)
        notes: list[str] = []
        if live.media.duration is not None and live.media.duration > MAX_SECONDS:
            notes.append(f"Only the first {MAX_SECONDS} seconds of the video are shown.")
        if count_videos(post) > 1:
            notes.append("Only one video or GIF plays; the others are still previews.")
        # The still-preview warning describes /image output, not this video.
        trimmed = _strip_warning(post, STILL_PREVIEWS)
        caption = self._caption(trimmed, assets, tuple(notes))
        buffer = BytesIO(result.data)
        buffer.name = f"x-{post.id}-{theme}.mp4"
        if live.media.animated:
            # Sent as an animation so Telegram loops it like the original GIF.
            await message.reply_animation(
                buffer,
                filename=buffer.name,
                caption=caption,
                reply_markup=markup,
                width=result.width,
                height=result.height,
                write_timeout=120,
            )
        else:
            await message.reply_video(
                buffer,
                filename=buffer.name,
                caption=caption,
                reply_markup=markup,
                width=result.width,
                height=result.height,
                supports_streaming=True,
                write_timeout=120,
            )

    def register(self, app: Application) -> None:  # type: ignore[type-arg]
        app.add_handler(CommandHandler(["start", "help"], self.help))
        app.add_handler(CommandHandler("id", self.identity))
        app.add_handler(CommandHandler("privacy", self.privacy))
        app.add_handler(CommandHandler("theme", self.theme))
        app.add_handler(CommandHandler("image", self.image))
        app.add_handler(CommandHandler("file", self.file))
        app.add_handler(CommandHandler("video", self.video))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.image))
        app.add_handler(MessageHandler(filters.COMMAND, self.help))


COMMANDS = [
    BotCommand("image", "Turn a public X post link into a photo"),
    BotCommand("file", "Get the original PNG file"),
    BotCommand("video", "Play a post's video or GIF inside its card"),
    BotCommand("theme", "Choose light or dark"),
    BotCommand("help", "Show help"),
    BotCommand("privacy", "Data handling and providers"),
    BotCommand("id", "Show your Telegram user ID"),
]


def _strip_warning(post: Post, warning: str) -> Post:
    def strip(item: Post | None) -> Post | None:
        if item is None:
            return None
        return replace(item, warnings=tuple(w for w in item.warnings if w != warning))

    stripped = strip(post)
    assert stripped is not None
    return replace(stripped, quote=strip(post.quote), parent=strip(post.parent))
