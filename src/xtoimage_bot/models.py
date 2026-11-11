"""Provider-independent post data. Everything from a provider is untrusted."""

from dataclasses import dataclass, field
from typing import Literal

Theme = Literal["light", "dark"]


@dataclass(frozen=True)
class Media:
    url: str
    kind: Literal["photo", "video"] = "photo"
    alt_text: str = ""


@dataclass(frozen=True)
class Post:
    id: str
    url: str
    author_name: str
    author_handle: str
    text: str
    created_at: str = ""
    avatar_url: str = ""
    media: tuple[Media, ...] = ()
    quote: "Post | None" = None
    # The post this one replies to, shown above it. Only one level is ever fetched.
    parent: "Post | None" = None
    likes: int | None = None
    reposts: int | None = None
    replies: int | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RenderAssets:
    """Only re-encoded, size-bounded PNG data URLs from our media downloader."""

    avatar: str = ""
    media: tuple[str, ...] = ()
    quote_avatar: str = ""
    quote_media: tuple[str, ...] = ()
    parent_avatar: str = ""
    parent_media: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)


class UserError(Exception):
    """Safe, static messages suitable for showing to a user."""
