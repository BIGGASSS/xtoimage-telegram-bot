import subprocess
import sys


def test_secret_bearing_child_logs_do_not_reach_stderr():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import logging
from xtoimage_bot.__main__ import configure_logging
configure_logging()
for name in ('telegram.ext.Updater', 'httpx._client', 'httpcore.http11', 'playwright.transport'):
    logging.getLogger(name).warning('SECRET-TOKEN private-url')
logging.getLogger('xtoimage_bot').info('safe operational event')
""",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "SECRET-TOKEN" not in result.stderr
    assert "private-url" not in result.stderr
    assert "safe operational event" in result.stderr
