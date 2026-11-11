"""Environment-only configuration. Secrets never appear in repr or error messages."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .models import Theme


def _secret(env: Mapping[str, str], name: str) -> str:
    value, filename = env.get(name, ""), env.get(f"{name}_FILE", "")
    if value and filename:
        raise ValueError(f"Set only {name} or {name}_FILE, not both.")
    if filename:
        try:
            value = Path(filename).read_text(encoding="utf-8").strip()
        except OSError:
            raise ValueError(f"Cannot read {name}_FILE.") from None
    return value.strip()


def _integer(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(env.get(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} must be an integer.") from None
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}.")
    return value


@dataclass(frozen=True)
class Config:
    bot_token: str = field(repr=False)
    provider: str = "fxtwitter"
    x_bearer_token: str = field(default="", repr=False)
    allowed_user_ids: frozenset[int] = frozenset()
    private_chats_only: bool = True
    default_theme: Theme = "light"
    max_concurrency: int = 2
    user_cooldown_seconds: int = 10
    chromium_executable_path: str | None = None
    ffmpeg_executable_path: str | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if environ is None else environ
        token = _secret(env, "TELEGRAM_BOT_TOKEN")
        if not token or ":" not in token:
            raise ValueError("Set TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN_FILE to a bot token.")
        provider = env.get("POST_PROVIDER", "fxtwitter").lower()
        if provider not in {"fxtwitter", "x_api"}:
            raise ValueError("POST_PROVIDER must be fxtwitter or x_api.")
        bearer = _secret(env, "X_BEARER_TOKEN")
        if provider == "x_api" and not bearer:
            raise ValueError("The x_api provider requires X_BEARER_TOKEN or X_BEARER_TOKEN_FILE.")
        theme = env.get("DEFAULT_THEME", "light").lower()
        if theme not in {"light", "dark"}:
            raise ValueError("DEFAULT_THEME must be light or dark.")
        private = env.get("PRIVATE_CHATS_ONLY", "true").lower()
        if private not in {"true", "false"}:
            raise ValueError("PRIVATE_CHATS_ONLY must be true or false.")
        try:
            allowlist = frozenset(
                int(part.strip())
                for part in env.get("ALLOWED_USER_IDS", "").split(",")
                if part.strip()
            )
        except ValueError:
            raise ValueError(
                "ALLOWED_USER_IDS must be comma-separated numeric Telegram IDs."
            ) from None
        if any(user_id <= 0 for user_id in allowlist):
            raise ValueError("ALLOWED_USER_IDS must contain positive Telegram user IDs.")
        return cls(
            bot_token=token,
            provider=provider,
            x_bearer_token=bearer,
            allowed_user_ids=allowlist,
            private_chats_only=private == "true",
            default_theme="dark" if theme == "dark" else "light",
            max_concurrency=_integer(env, "MAX_CONCURRENCY", 2, 1, 4),
            user_cooldown_seconds=_integer(env, "USER_COOLDOWN_SECONDS", 10, 1, 3600),
            chromium_executable_path=env.get("CHROMIUM_EXECUTABLE_PATH") or None,
            ffmpeg_executable_path=env.get("FFMPEG_EXECUTABLE_PATH") or None,
        )
