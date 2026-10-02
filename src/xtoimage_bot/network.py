"""Bounded outbound requests to fixed trusted hosts; no redirects or arbitrary fetches."""

import asyncio
import json
from typing import Any
from urllib.parse import urlsplit

import httpx

from .models import UserError

API_HOSTS = frozenset({"api.fxtwitter.com", "api.x.com"})
MEDIA_HOSTS = frozenset({"pbs.twimg.com"})


def validate_remote_url(url: str, hosts: frozenset[str]) -> None:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in hosts
            or parsed.port is not None
            or parsed.username is not None
            or parsed.password is not None
            or "\\" in url
            or len(url) > 2048
        ):
            raise ValueError
    except ValueError:
        raise UserError("The post contains an unsupported media address.") from None


def _check_status(status: int) -> None:
    if status == 429:
        raise UserError("The post provider is rate-limited. Please try again in a few minutes.")
    if status in {401, 403}:
        raise UserError(
            "The provider cannot access this post. "
            "It may be protected, or provider access needs attention."
        )
    if status in {404, 410}:
        raise UserError("This post is unavailable. It may have been deleted or made private.")
    if not 200 <= status < 300:
        raise UserError("The post provider is unavailable right now. Please try again later.")


async def fetch_bytes(
    client: httpx.AsyncClient,
    url: str,
    *,
    hosts: frozenset[str],
    limit: int,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> bytes:
    validate_remote_url(url, hosts)
    try:
        async with asyncio.timeout(20):
            async with client.stream(
                "GET", url, headers=headers, params=params, follow_redirects=False, timeout=12
            ) as response:
                _check_status(response.status_code)
                length = response.headers.get("content-length", "")
                if length.isdecimal() and int(length) > limit:
                    raise UserError("The post or its media is too large to process safely.")
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    body.extend(chunk)
                    if len(body) > limit:
                        raise UserError("The post or its media is too large to process safely.")
                return bytes(body)
    except (TimeoutError, httpx.TimeoutException):
        raise UserError("The post provider took too long to respond. Please try again.") from None
    except httpx.HTTPError:
        raise UserError("Could not reach the post provider. Please try again later.") from None


async def fetch_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    body = await fetch_bytes(
        client, url, hosts=API_HOSTS, limit=2_000_000, headers=headers, params=params
    )
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise UserError("The post provider returned an unreadable response.") from None
    if not isinstance(data, dict):
        raise UserError("The post provider returned an unreadable response.")
    return data
