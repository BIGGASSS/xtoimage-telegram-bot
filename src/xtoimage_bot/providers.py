"""Small, defensive adapters for public posts; no browser scraping or credentials fallback."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from typing import cast
from urllib.parse import urlsplit

import httpx

from .models import Media, Post, UserError
from .network import fetch_json

MAX_TEXT = 12_000
_ID = re.compile(r"[1-9][0-9]{0,19}\Z", re.ASCII)
_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}\Z", re.ASCII)
_MALFORMED = "The post service returned an incomplete or invalid response. Please try again later."
_UNAVAILABLE = "This post is unavailable, deleted, or private. Only public posts are supported."
_QUOTE_UNAVAILABLE = "The quoted post is unavailable or could not be safely rendered."

_PARENT_UNAVAILABLE = "The post this replies to is unavailable or could not be safely rendered."
_NESTED_QUOTE = "A further nested quoted post is not shown."
_CONVERSATION = "This is a single-post image; the surrounding conversation is not shown."
_EARLIER = "Earlier posts in this conversation are not shown."
_PARENT_QUOTE = "Its quoted post is not shown."


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise UserError(_MALFORMED)
    return cast(dict[str, object], value)


def _optional_mapping(value: object) -> dict[str, object]:
    return {} if value is None else _mapping(value)


def _list(value: object, *, maximum: int = 128) -> list[object]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise UserError(_MALFORMED)
    return cast(list[object], value)


def _text(value: object, *, limit: int = 256, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise UserError(_MALFORMED)
    if len(value) > limit:
        raise UserError(
            "This post exceeds the supported text or metadata size; it was not truncated."
        )
    return value


def _post_id(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise UserError("Please send a valid public X post link.")
    return value


def _count(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise UserError(_MALFORMED)
    return value


def _image_url(value: object) -> str:
    url = _text(value, limit=2048)
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname == "pbs.twimg.com"
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
            and not parsed.fragment
            and not any(ord(char) < 32 for char in url)
        )
    except ValueError:
        valid = False
    if not valid:
        return ""
    return url


def _identity(post: Mapping[str, object], author: Mapping[str, object]) -> tuple[str, str, str]:
    post_id = _post_id(post.get("id"))
    protected = author.get("protected", False)
    if type(protected) is not bool:
        raise UserError(_MALFORMED)
    if protected or post.get("withheld"):
        raise UserError(_UNAVAILABLE)
    handle = _text(author.get("screen_name", author.get("username")), required=True, limit=15)
    if not _HANDLE.fullmatch(handle):
        raise UserError(_MALFORMED)
    name = _text(author.get("name"), required=True)
    return post_id, handle, name


def _media(
    items: list[object], warnings: list[str], *, official: bool = False
) -> tuple[Media, ...]:
    result: list[Media] = []
    if len(items) > 4:
        warnings.append("Only the first four media attachments are shown.")
    for raw in items[:4]:
        item = _mapping(raw)
        kind = item.get("type")
        alt = _text(item.get("alt_text" if official else "altText"), limit=2048)
        if kind == "photo":
            url = _image_url(item.get("url"))
            if url:
                result.append(Media(url=url, kind="photo", alt_text=alt))
            else:
                warnings.append("A photo attachment could not be safely loaded.")
        elif kind in ("video", "gif", "animated_gif"):
            key = "preview_image_url" if official else "thumbnail_url"
            url = _image_url(item.get(key))
            if url:
                result.append(Media(url=url, kind="video", alt_text=alt))
                warnings.append("Videos and animated GIFs are shown as still previews.")
            else:
                warnings.append("A video attachment has no usable still preview.")
        else:
            warnings.append("An unsupported media attachment is not shown.")
    return tuple(result)


def _fx_post(raw: object, *, allow_quote: bool) -> Post:
    data = _mapping(raw)
    if data.get("type") == "tombstone":
        raise UserError(_UNAVAILABLE)
    if data.get("type") != "status":
        raise UserError(_MALFORMED)
    author = _mapping(data.get("author"))
    post_id, handle, name = _identity(data, author)
    warnings: list[str] = []
    raw_text = _optional_mapping(data.get("raw_text"))
    # FxEmbed's display text omits hidden attachment/quote links and expands real URLs.
    # Fall back only when absent: an empty display text is valid for media-only posts.
    text = _text(data.get("text", raw_text.get("text")), limit=MAX_TEXT)
    if not text and not data.get("media"):
        raise UserError(_MALFORMED)
    media = _optional_mapping(data.get("media"))
    if "all" in media:
        media_items = _list(media["all"])
    else:
        media_items = _list(media.get("photos")) + _list(media.get("videos"))
    images = _media(media_items, warnings)
    if data.get("poll") is not None:
        warnings.append("This post contains a poll; poll choices and results are not shown.")
    if data.get("article") is not None:
        warnings.append("This post links to an article; the full article is not shown.")
    if data.get("replying_to") or data.get("replying_to_status") or data.get("thread"):
        warnings.append(_CONVERSATION)
    if media.get("external") is not None:
        warnings.append("An external media or link preview is not shown.")
    quote: Post | None = None
    if data.get("quote") is not None:
        if allow_quote:
            try:
                quote = _fx_post(data["quote"], allow_quote=False)
            except UserError:
                warnings.append(_QUOTE_UNAVAILABLE)
        else:
            warnings.append(_NESTED_QUOTE)
    avatar = _image_url(author.get("avatar_url"))
    if not avatar:
        warnings.append("The author's profile picture is unavailable.")
    return Post(
        id=post_id,
        url=f"https://x.com/{handle}/status/{post_id}",
        author_name=name,
        author_handle=handle,
        text=text,
        created_at=_text(data.get("created_at"), limit=128),
        avatar_url=avatar,
        media=images,
        quote=quote,
        likes=_count(data.get("likes")),
        reposts=_count(data.get("reposts")),
        replies=_count(data.get("replies")),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _fx_parent_id(data: Mapping[str, object]) -> str | None:
    """The replied-to post ID, from the current object shape or the older flat fields."""
    replying_to = data.get("replying_to")
    if isinstance(replying_to, dict):
        value = cast(dict[str, object], replying_to).get("status")
    else:
        value = data.get("replying_to_status")
    return value if isinstance(value, str) and value else None


def _as_parent(parent: Post) -> Post:
    """A replied-to post is shown one level deep: reword warnings for that context."""
    reworded = {_CONVERSATION: _EARLIER, _NESTED_QUOTE: _PARENT_QUOTE}
    return replace(
        parent,
        quote=None,
        parent=None,
        warnings=tuple(dict.fromkeys(reworded.get(w, w) for w in parent.warnings)),
    )


def _with_parent(result: Post, parent: Post | None, *, thread: bool) -> Post:
    warnings = [w for w in result.warnings if w != _CONVERSATION]
    if parent is None:
        warnings.append(_PARENT_UNAVAILABLE)
    if thread:
        warnings.append(_CONVERSATION)
    return replace(result, parent=parent, warnings=tuple(dict.fromkeys(warnings)))


class FxTwitterProvider:
    """Public, unauthenticated FxEmbed v2; no login/private-post fallback."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def _status(self, post_id: str) -> dict[str, object]:
        data = _mapping(
            await fetch_json(
                self._client,
                f"https://api.fxtwitter.com/2/status/{post_id}",
                headers={"User-Agent": "xtoimage-telegram-bot/0.1 (public-post image renderer)"},
            )
        )
        code = data.get("code")
        if type(code) is not int:
            raise UserError(_MALFORMED)
        if code in (401, 403, 404):
            raise UserError(_UNAVAILABLE)
        if code == 429:
            raise UserError("The post service is rate limited. Please try again later.")
        if code != 200:
            raise UserError("The post service is temporarily unavailable. Please try again later.")
        return data

    async def _parent(self, parent_id: str, child_id: str) -> Post | None:
        """Best effort: a missing parent never prevents the reply itself from rendering."""
        try:
            parent_id = _post_id(parent_id)
            if parent_id == child_id:
                return None
            data = await self._status(parent_id)
            parent = _fx_post(data.get("status"), allow_quote=False)
        except UserError:
            return None
        return _as_parent(parent) if parent.id == parent_id else None

    async def fetch(self, post_id: str) -> Post:
        post_id = _post_id(post_id)
        data = await self._status(post_id)
        status = _mapping(data.get("status"))
        result = _fx_post(status, allow_quote=True)
        if result.id != post_id:
            raise UserError(_MALFORMED)
        parent_id = _fx_parent_id(status)
        if parent_id is not None:
            parent = await self._parent(parent_id, post_id)
            thread = bool(data.get("thread") or status.get("thread"))
            return _with_parent(result, parent, thread=thread)
        if data.get("thread"):
            result = replace(
                result,
                warnings=tuple(
                    dict.fromkeys(
                        (
                            *result.warnings,
                            _CONVERSATION,
                        )
                    )
                ),
            )
        return result


def _index(value: object, key: str = "id") -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for raw in _list(value):
        item = _mapping(raw)
        identity = _text(item.get(key), limit=128, required=True)
        if identity in result:
            raise UserError(_MALFORMED)
        result[identity] = item
    return result


def _x_post(
    raw: object,
    users: Mapping[str, dict[str, object]],
    media: Mapping[str, dict[str, object]],
    posts: Mapping[str, dict[str, object]],
    *,
    allow_quote: bool,
    allow_parent: bool = False,
) -> Post:
    data = _mapping(raw)
    author_id = _text(data.get("author_id"), required=True, limit=128)
    author = users.get(author_id)
    if author is None:
        raise UserError(_MALFORMED)
    post_id, handle, name = _identity(data, author)
    warnings: list[str] = []
    # X's current schema uses post naming; retain legacy response compatibility.
    note = _optional_mapping(data.get("note_post", data.get("note_tweet")))
    text = _text(note.get("text", data.get("text")), limit=MAX_TEXT)
    attachments = _optional_mapping(data.get("attachments"))
    media_items: list[object] = []
    for key in _list(attachments.get("media_keys")):
        identity = _text(key, required=True, limit=128)
        item = media.get(identity)
        if item is None:
            warnings.append("A media attachment is unavailable.")
        else:
            media_items.append(item)
    images = _media(media_items, warnings, official=True)
    if not text and not media_items:
        raise UserError(_MALFORMED)
    if attachments.get("poll_ids"):
        warnings.append("This post contains a poll; poll choices and results are not shown.")
    if data.get("article") is not None or data.get("article_title") is not None:
        warnings.append("This post links to an article; the full article is not shown.")
    references = _list(data.get("referenced_posts", data.get("referenced_tweets")))
    quote: Post | None = None
    parent: Post | None = None
    replying = False
    for value in references:
        ref = _mapping(value)
        if ref.get("type") == "quoted":
            if not allow_quote:
                warnings.append(_NESTED_QUOTE)
            elif quote is None:
                try:
                    quote_id = _post_id(ref.get("id"))
                    quote = _x_post(posts.get(quote_id), users, media, posts, allow_quote=False)
                except UserError:
                    warnings.append(_QUOTE_UNAVAILABLE)
        elif ref.get("type") == "replied_to" and allow_parent and not replying:
            replying = True
            try:
                parent_id = _post_id(ref.get("id"))
                if parent_id == post_id:
                    raise UserError(_MALFORMED)
                parent = _as_parent(
                    _x_post(posts.get(parent_id), users, media, posts, allow_quote=False)
                )
            except UserError:
                warnings.append(_PARENT_UNAVAILABLE)
        elif ref.get("type") in ("replied_to", "retweeted", "reposted"):
            warnings.append(_CONVERSATION)
    avatar = _image_url(author.get("profile_image_url"))
    if not avatar:
        warnings.append("The author's profile picture is unavailable.")
    metrics = _optional_mapping(data.get("public_metrics"))
    return Post(
        id=post_id,
        url=f"https://x.com/{handle}/status/{post_id}",
        author_name=name,
        author_handle=handle,
        text=text,
        created_at=_text(data.get("created_at"), limit=128),
        avatar_url=avatar,
        media=images,
        quote=quote,
        parent=parent,
        likes=_count(metrics.get("like_count")),
        reposts=_count(metrics.get("repost_count", metrics.get("retweet_count"))),
        replies=_count(metrics.get("reply_count")),
        warnings=tuple(dict.fromkeys(warnings)),
    )


class XApiProvider:
    """Official X API; caller supplies a Bearer token through local configuration."""

    def __init__(self, client: httpx.AsyncClient, bearer_token: str) -> None:
        if not bearer_token or any(ord(char) < 33 for char in bearer_token):
            raise UserError("The official X provider requires a valid local Bearer token.")
        self._client = client
        self._bearer_token = bearer_token

    async def fetch(self, post_id: str) -> Post:
        post_id = _post_id(post_id)
        params = {
            "post.fields": "created_at,public_metrics,attachments,note_post,article,withheld",
            # in_reply_to_user_id expands the replied-to author so the parent can be shown.
            "expansions": "author_id,attachments.media_keys,referenced_posts,in_reply_to_user_id",
            "user.fields": "name,username,profile_image_url,protected",
            "media.fields": "type,url,preview_image_url,alt_text",
        }
        data = _mapping(
            await fetch_json(
                self._client,
                f"https://api.x.com/2/tweets/{post_id}",
                headers={"Authorization": f"Bearer {self._bearer_token}"},
                params=params,
            )
        )
        if data.get("data") is None:
            raise UserError(_UNAVAILABLE)
        includes = _optional_mapping(data.get("includes"))
        result = _x_post(
            data["data"],
            _index(includes.get("users")),
            _index(includes.get("media"), "media_key"),
            _index(includes.get("posts", includes.get("tweets"))),
            allow_quote=True,
            allow_parent=True,
        )
        if result.id != post_id:
            raise UserError(_MALFORMED)
        return result
