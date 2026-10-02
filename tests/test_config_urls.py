import pytest

from xtoimage_bot.config import Config
from xtoimage_bot.models import UserError
from xtoimage_bot.urls import extract_post_link, parse_post_url

TOKEN = "123:test-token"


def test_config_defaults_and_secrets_repr():
    config = Config.from_env({"TELEGRAM_BOT_TOKEN": TOKEN})
    assert config.private_chats_only and config.provider == "fxtwitter"
    assert TOKEN not in repr(config)


def test_secret_file(tmp_path):
    token = tmp_path / "token"
    token.write_text(TOKEN)
    assert Config.from_env({"TELEGRAM_BOT_TOKEN_FILE": str(token)}).bot_token == TOKEN


@pytest.mark.parametrize(
    "extra",
    [
        {"POST_PROVIDER": "unknown"},
        {"POST_PROVIDER": "x_api"},
        {"MAX_CONCURRENCY": "20"},
        {"USER_COOLDOWN_SECONDS": "0"},
        {"ALLOWED_USER_IDS": "name"},
        {"ALLOWED_USER_IDS": "-2"},
        {"DEFAULT_THEME": "neon"},
        {"PRIVATE_CHATS_ONLY": "maybe"},
        {"TELEGRAM_BOT_TOKEN_FILE": "/unused"},
    ],
)
def test_invalid_config(extra):
    with pytest.raises(ValueError):
        Config.from_env({"TELEGRAM_BOT_TOKEN": TOKEN, **extra})


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/jack/status/20",
        "http://twitter.com/jack/status/20?s=20",
        "https://mobile.twitter.com/jack/status/20/photo/1",
        "https://x.com/i/web/status/20",
        "https://x.com/i/status/20",
        "https://www.x.com/jack/status/20#fragment",
    ],
)
def test_accepted_urls(url):
    assert parse_post_url(url).id == "20"


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com.evil.org/jack/status/20",
        "https://x.com@evil.org/jack/status/20",
        "https://evil.org@x.com/jack/status/20",
        "https://127.0.0.1/jack/status/20",
        "file:///etc/passwd",
        "https://x.com:443/jack/status/20",
        "https://x.com/jack",
        "https://x.com/jack/status/notanumber",
        "https://x.com/jack/status/20/elsewhere",
        "https://x.com/../../status/20",
        "https://x.com/jack/status/20%2f..",
        "https://x.com\\@evil.org/jack/status/20",
        "https://t.co/shortlink",
    ],
)
def test_rejected_urls(url):
    with pytest.raises(UserError):
        parse_post_url(url)


def test_extract_one_link_and_punctuation():
    assert extract_post_link("Please screenshot (https://x.com/jack/status/20).").id == "20"
    with pytest.raises(UserError):
        extract_post_link("https://x.com/jack/status/20 https://x.com/jack/status/21")
