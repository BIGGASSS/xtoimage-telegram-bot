"""Telegram handlers. Provider, downloader and renderer remain independently testable."""

import asyncio
import logging
import time
from collections import OrderedDict
from io import BytesIO
from typing import Protocol

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ChatType
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .config import Config
from .models import Post, RenderAssets, Theme, UserError
from .urls import extract_post_link

LOG = logging.getLogger(__name__)
MAX_USERS = 10_000
HELP = (
    "Send me one public x.com or twitter.com post link and I’ll turn it into an image.\n\n"
    "/image <link> — shareable photo\n"
    "/file <link> — original PNG, without Telegram photo compression\n"
    "/theme light or /theme dark — choose your style\n"
    "/id — show your numeric Telegram user ID\n"
    "/privacy — how post links and images are handled\n\n"
    "Photos and video thumbnails are supported, with one quoted post. Replies include the "
    "post they reply to. Private/deleted posts cannot be fetched. Videos are still images; "
    "longer threads and polls are not fully captured. Very long posts may not fit in one image."
)


class Provider(Protocol):
    async def fetch(self, post_id: str) -> Post: ...


class AssetLoader(Protocol):
    async def load(self, post: Post) -> RenderAssets: ...


class Renderer(Protocol):
    async def render(self, post: Post, assets: RenderAssets, theme: Theme = "light") -> bytes: ...


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
        self, config: Config, provider: Provider, media: AssetLoader, renderer: Renderer
    ) -> None:
        self.config, self.provider, self.media, self.renderer = config, provider, media, renderer
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
            "Rendered images and messages go through Telegram. "
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
        await self.convert(update, context, document=False)

    async def file(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.convert(update, context, document=True)

    async def convert(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, *, document: bool
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
            self.admission.enter(user.id)
            admitted = True
            async with asyncio.timeout(90):
                await context.bot.send_chat_action(message.chat_id, ChatAction.UPLOAD_PHOTO)
                post = await self.provider.fetch(link.id)
                assets = await self.media.load(post)
                theme = self.themes.get(user.id, self.config.default_theme)
                png = await self.renderer.render(post, assets, theme)
                if len(png) > 45_000_000:
                    raise UserError("This image is too large to send safely.")
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
                        (*post.warnings, *parent_warnings, *quote_warnings, *assets.warnings)
                    )
                )
                caption = f"@{post.author_handle}\n{post.url}"
                if warnings:
                    caption += "\n\n" + "\n".join(warnings)
                # Telegram photo/file captions are limited to 1024 characters.
                if len(caption) > 1000:
                    caption = caption[:997] + "…"
                markup = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Open original post", url=post.url)]]
                )
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
                else:
                    try:
                        await message.reply_photo(
                            buffer, caption=caption, reply_markup=markup, write_timeout=45
                        )
                    except BadRequest as error:
                        # Telegram may reject a technically valid but unusually shaped PNG.
                        reason = str(error).lower()
                        if not any(
                            term in reason
                            for term in (
                                "photo_invalid_dimensions",
                                "photo_invalid",
                                "image_process_failed",
                            )
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
                LOG.info("render_delivered")
        except UserError as error:
            await message.reply_text(str(error))
        except TimeoutError:
            await message.reply_text(
                "That image took too long to prepare. Please try again shortly."
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

    def register(self, app: Application) -> None:  # type: ignore[type-arg]
        app.add_handler(CommandHandler(["start", "help"], self.help))
        app.add_handler(CommandHandler("id", self.identity))
        app.add_handler(CommandHandler("privacy", self.privacy))
        app.add_handler(CommandHandler("theme", self.theme))
        app.add_handler(CommandHandler("image", self.image))
        app.add_handler(CommandHandler("file", self.file))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.image))
        app.add_handler(MessageHandler(filters.COMMAND, self.help))


COMMANDS = [
    BotCommand("image", "Turn a public X post link into a photo"),
    BotCommand("file", "Get the original PNG file"),
    BotCommand("theme", "Choose light or dark"),
    BotCommand("help", "Show help"),
    BotCommand("privacy", "Data handling and providers"),
    BotCommand("id", "Show your Telegram user ID"),
]
