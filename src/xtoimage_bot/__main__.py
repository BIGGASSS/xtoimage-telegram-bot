"""Run one long-polling instance; no public webhook, database or paid host required."""

import asyncio
import logging

import httpx
from telegram import Update
from telegram.ext import Application

from .bot import COMMANDS, BotService
from .config import Config
from .media import MediaLoader
from .providers import FxTwitterProvider, XApiProvider
from .render import Renderer


def configure_logging() -> None:
    """Drop third-party logs, including descendants and logging.lastResort."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for name in ("httpx", "httpcore", "telegram", "playwright"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.disabled = True
        logger.propagate = False


def main() -> None:
    configure_logging()
    try:
        config = Config.from_env()
    except ValueError as error:
        raise SystemExit(str(error)) from None
    client = httpx.AsyncClient(
        follow_redirects=False,
        limits=httpx.Limits(max_connections=12, max_keepalive_connections=6),
    )
    renderer = Renderer(
        executable_path=config.chromium_executable_path, max_concurrency=config.max_concurrency
    )
    provider = (
        XApiProvider(client, config.x_bearer_token)
        if config.provider == "x_api"
        else FxTwitterProvider(client)
    )
    service = BotService(config, provider, MediaLoader(client), renderer)

    async def startup(app: Application) -> None:  # type: ignore[type-arg]
        webhook = await app.bot.get_webhook_info()
        if webhook.url:
            raise RuntimeError(
                "This bot has an existing webhook. Remove it intentionally before long polling."
            )
        await renderer.start()
        await app.bot.set_my_commands(COMMANDS)
        logging.getLogger(__name__).info("bot_started provider=%s", config.provider)

    async def shutdown(app: Application) -> None:  # type: ignore[type-arg]
        await renderer.close()
        await client.aclose()

    async def error_handler(update: object, context: object) -> None:
        logging.getLogger(__name__).error("telegram_update_failed")

    app = (
        Application.builder()
        .token(config.bot_token)
        .concurrent_updates(False)
        .update_queue(asyncio.Queue(maxsize=32))
        .post_init(startup)
        .post_shutdown(shutdown)
        .build()
    )
    service.register(app)
    app.add_error_handler(error_handler)
    try:
        app.run_polling(
            allowed_updates=[Update.MESSAGE], drop_pending_updates=False, bootstrap_retries=0
        )
    except Exception as error:
        # No traceback or exception text, which may contain secrets, provider data or URLs.
        raise SystemExit(
            f"Bot stopped ({type(error).__name__}). "
            "Check credentials, webhook and network configuration."
        ) from None


if __name__ == "__main__":
    main()
