"""Strict X URL extraction; never fetch submitted URLs or follow short links."""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .models import UserError

HOSTS = frozenset(
    {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com", "mobile.x.com"}
)
PATH = re.compile(
    r"/(?:([A-Za-z0-9_]{1,15})/status|i/status|i/web/status)/([0-9]{2,20})(?:/(?:photo|video)/[1-4])?/?$"
)
URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


@dataclass(frozen=True)
class PostLink:
    id: str
    url: str


def parse_post_url(raw: str) -> PostLink:
    try:
        parsed = urlsplit(raw)
        if (
            parsed.scheme not in {"https", "http"}
            or parsed.hostname not in HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or "\\" in raw
        ):
            raise ValueError
        match = PATH.fullmatch(parsed.path)
        if not match:
            raise ValueError
    except ValueError:
        raise UserError(
            "Send a public X post link like https://x.com/username/status/1234567890."
        ) from None
    author, post_id = match.groups()
    return PostLink(post_id, f"https://x.com/{author or 'i'}/status/{post_id}")


def extract_post_link(text: str) -> PostLink:
    candidates = URL.findall(text)
    if len(candidates) != 1:
        raise UserError("Please send one public X post link at a time.")
    return parse_post_url(candidates[0].rstrip(".,;!?)'\""))
