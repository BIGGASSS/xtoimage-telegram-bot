import asyncio
import logging

from telegram.ext import Application

from xtoimage_bot.__main__ import configure_logging


def test_sensitive_child_logs_do_not_reach_last_resort(capsys):
    configure_logging()
    for name in ("telegram.ext.Updater", "httpx.client", "httpcore.connection", "playwright.api"):
        try:
            raise RuntimeError("SYNTHETIC_SECRET_IN_EXCEPTION")
        except RuntimeError:
            logging.getLogger(name).error("SYNTHETIC_SECRET_IN_URL", exc_info=True)
    captured = capsys.readouterr()
    assert "SYNTHETIC_SECRET" not in captured.err + captured.out


def test_polling_dispatch_is_sequential_and_queue_is_bounded():
    app = (
        Application.builder()
        .token("123:test")
        .concurrent_updates(False)
        .update_queue(asyncio.Queue(maxsize=32))
        .build()
    )
    assert app.concurrent_updates == 1
    assert app.update_queue.maxsize == 32
