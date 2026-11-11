"""Provider-independent post data. Everything from a provider is untrusted."""

from dataclasses import dataclass, field
from typing import Literal

Theme = Literal["light", "dark"]


@dataclass(frozen=True)
class Media:
    url: str
    kind: Literal["photo", "video"] = "photo"
    alt_text: str = ""
    # For videos and GIFs: an allowlisted MP4 used by /video. Empty when none is usable.
    video_url: str = ""
    animated: bool = False
    duration: float | None = None


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


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class Frame:
    """A rendered card plus where the playing video goes, in card pixels.

    `slot` is the box the clip is fitted into. `cover` is an RGBA PNG of the card at
    `region`, transparent exactly where the slot is visible: laid over the clip, it
    keeps the card's rounded corners and anti-aliased edges.
    """

    png: bytes
    width: int
    height: int
    slot: Box
    region: Box
    cover: bytes


class UserError(Exception):
    """Safe, static messages suitable for showing to a user."""
