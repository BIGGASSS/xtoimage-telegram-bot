"""Offline provider-contract tests. Public Fx fixtures were checked 2026-10-02 UTC."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from xtoimage_bot.models import RenderAssets, UserError
from xtoimage_bot.providers import FxTwitterProvider, XApiProvider
from xtoimage_bot.render import build_html

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str = "fx_jack.json") -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())  # type: ignore[no-any-return]


def client_for(payload: object, status: int = 200) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json=payload, request=request)
        )
    )


async def test_fx_live_text_fixture() -> None:
    async with client_for(fixture()) as client:
        post = await FxTwitterProvider(client).fetch("20")
    assert post.id == "20"
    assert post.url == "https://x.com/jack/status/20"
    assert post.author_handle == "jack"
    assert post.text == "just setting up my twttr"
    assert post.reposts == 124658
    assert post.warnings == ()


async def test_fx_live_photo_fixture_omits_hidden_attachment_link() -> None:
    payload = fixture("fx_photo.json")
    async with client_for(payload) as client:
        post = await FxTwitterProvider(client).fetch("463440424141459456")
    assert post.text == payload["status"]["text"]
    assert len(post.media) == 1
    assert post.media[0].kind == "photo"
    assert "t.co/YuKy2rcjyU" not in build_html(post, RenderAssets())


async def test_fx_media_only_post_keeps_empty_display_text() -> None:
    payload = fixture("fx_photo.json")
    payload["status"]["text"] = ""
    payload["status"]["raw_text"]["text"] = "http://t.co/YuKy2rcjyU"
    async with client_for(payload) as client:
        post = await FxTwitterProvider(client).fetch("463440424141459456")
    assert post.text == ""
    assert len(post.media) == 1


@pytest.mark.parametrize("raw_text_only", [False, True])
async def test_fx_preserves_visible_links_and_line_breaks(raw_text_only: bool) -> None:
    payload = fixture("fx_photo.json")
    text = "Read https://example.com/article\n\nAlso https://t.co/authored"
    payload["status"]["text"] = text
    payload["status"]["raw_text"]["text"] = text + " http://t.co/YuKy2rcjyU"
    if raw_text_only:
        del payload["status"]["text"]
        payload["status"]["raw_text"]["text"] = text
    async with client_for(payload) as client:
        post = await FxTwitterProvider(client).fetch("463440424141459456")
    assert post.text == text


async def test_fx_quote_also_uses_display_text() -> None:
    payload = fixture()
    payload["status"]["quote"] = fixture("fx_photo.json")["status"]
    async with client_for(payload) as client:
        post = await FxTwitterProvider(client).fetch("20")
    assert post.quote and post.quote.text == payload["status"]["quote"]["text"]
    assert len(post.quote.media) == 1
    assert "t.co/YuKy2rcjyU" not in build_html(post, RenderAssets())


async def test_fx_endpoint_is_fixed_and_authoritative_identity() -> None:
    payload = fixture()
    payload["status"]["url"] = "https://malicious.example/post"

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.fxtwitter.com/2/status/20"
        assert "xtoimage-telegram-bot" in request.headers["user-agent"]
        assert "authorization" not in request.headers
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        post = await FxTwitterProvider(client).fetch("20")
    assert post.url == "https://x.com/jack/status/20"


@pytest.mark.parametrize("post_id", ["", "0", "00", "../20", "20?x=y", "２0", "9" * 21])
async def test_invalid_id_does_not_request(post_id: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Invalid ID must never make a request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for provider in (FxTwitterProvider(client), XApiProvider(client, "local-test-token")):
            with pytest.raises(UserError):
                await provider.fetch(post_id)


@pytest.mark.parametrize(
    "change", ["mismatch", "long_text", "long_raw_text", "long_name", "handle", "private"]
)
async def test_fx_rejects_unsafe_post(change: str) -> None:
    payload = fixture()
    status = payload["status"]
    if change == "mismatch":
        status["id"] = "21"
    elif change == "long_text":
        status["text"] = "x" * 12_001
    elif change == "long_raw_text":
        del status["text"]
        status["raw_text"]["text"] = "x" * 12_001
    elif change == "long_name":
        status["author"]["name"] = "x" * 257
    elif change == "handle":
        status["author"]["screen_name"] = "bad/path"
    else:
        status["author"]["protected"] = True
    async with client_for(payload) as client:
        with pytest.raises(UserError):
            await FxTwitterProvider(client).fetch("20")


@pytest.mark.parametrize(
    "payload", [[], {}, {"code": 200}, {"code": "200"}, {"code": 200, "status": 7}]
)
async def test_fx_malformed(payload: object) -> None:
    async with client_for(payload) as client:
        with pytest.raises(UserError):
            await FxTwitterProvider(client).fetch("20")


@pytest.mark.parametrize("code", [401, 403, 404, 429, 500])
async def test_fx_envelope_error_is_not_echoed(code: int) -> None:
    async with client_for({"code": code, "message": "secret-private-server-detail"}) as client:
        with pytest.raises(UserError) as exc:
            await FxTwitterProvider(client).fetch("20")
    assert "secret-private-server-detail" not in str(exc.value)


@pytest.mark.parametrize("http_status", [401, 403, 404, 429, 500])
async def test_fx_http_error(http_status: int) -> None:
    async with client_for({"private": "internal"}, status=http_status) as client:
        with pytest.raises(UserError) as exc:
            await FxTwitterProvider(client).fetch("20")
    assert "internal" not in str(exc.value)


async def test_fx_quote_tombstone_is_explicit() -> None:
    payload = fixture()
    payload["status"]["quote"] = {"type": "tombstone", "reason": "private"}
    async with client_for(payload) as client:
        post = await FxTwitterProvider(client).fetch("20")
    assert post.quote is None
    assert any("quoted post is unavailable" in warning for warning in post.warnings)


async def test_fx_envelope_thread_is_not_silently_omitted() -> None:
    payload = fixture()
    payload["thread"] = [{"id": "21"}]
    async with client_for(payload) as client:
        post = await FxTwitterProvider(client).fetch("20")
    assert any("conversation is not shown" in warning for warning in post.warnings)


@pytest.mark.parametrize("body", ["not json", '{"code":'])
async def test_invalid_json_is_safe(body: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    ) as client:
        with pytest.raises(UserError):
            await FxTwitterProvider(client).fetch("20")


async def test_redirect_is_not_followed() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": "https://evil.example/"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UserError):
            await XApiProvider(client, "local-test-token").fetch("20")
    assert calls == 1


async def test_fx_quote_one_level_and_unsupported_features() -> None:
    payload = fixture()
    quote = copy.deepcopy(payload["status"])
    quote["id"] = "21"
    quote["quote"] = copy.deepcopy(quote)
    payload["status"].update({"quote": quote, "poll": {}, "article": {}, "replying_to": "user"})
    async with client_for(payload) as client:
        post = await FxTwitterProvider(client).fetch("20")
    assert post.quote and post.quote.id == "21" and post.quote.quote is None
    assert any("nested quoted post" in warning for warning in post.quote.warnings)
    assert any("poll" in warning for warning in post.warnings)
    assert any("article" in warning for warning in post.warnings)
    assert any("conversation" in warning for warning in post.warnings)


async def test_fx_media_is_bounded_and_video_labeled() -> None:
    payload = fixture()
    payload["status"]["media"] = {
        "all": [
            {"type": "video", "thumbnail_url": "https://pbs.twimg.com/media/test.jpg"},
            {"type": "photo", "url": "http://127.0.0.1/private"},
            {"type": "photo", "url": "https://pbs.twimg.com@evil.example/a"},
            {"type": "photo", "url": "https://pbs.twimg.com/media/good.jpg"},
            {"type": "photo", "url": "https://pbs.twimg.com/media/fifth.jpg"},
        ]
    }
    async with client_for(payload) as client:
        post = await FxTwitterProvider(client).fetch("20")
    assert len(post.media) == 2
    assert post.media[0].kind == "video"
    assert any("first four" in warning for warning in post.warnings)
    assert any("still previews" in warning for warning in post.warnings)


def x_payload(*, legacy: bool = False) -> dict[str, Any]:
    ref_key = "referenced_tweets" if legacy else "referenced_posts"
    posts_key = "tweets" if legacy else "posts"
    note_key = "note_tweet" if legacy else "note_post"
    count_key = "retweet_count" if legacy else "repost_count"
    return {
        "data": {
            "id": "20",
            "author_id": "12",
            "text": "short",
            note_key: {"text": "Full, unmodified long-form post"},
            "created_at": "2006-03-21T20:50:14Z",
            "public_metrics": {"like_count": 2, count_key: 3, "reply_count": 4},
            "attachments": {"media_keys": ["media1"], "poll_ids": ["poll1"]},
            ref_key: [{"type": "quoted", "id": "21"}],
        },
        "includes": {
            "users": [
                {
                    "id": "12",
                    "name": "Fixture Author",
                    "username": "fixture_author",
                    "protected": False,
                    "profile_image_url": "https://pbs.twimg.com/profile_images/test.jpg",
                }
            ],
            "media": [
                {
                    "media_key": "media1",
                    "type": "video",
                    "preview_image_url": "https://pbs.twimg.com/media/test.jpg",
                }
            ],
            posts_key: [{"id": "21", "author_id": "12", "text": "Quoted fixture post"}],
        },
    }


@pytest.mark.parametrize("legacy", [True, False])
async def test_x_normalizes_both_documented_naming_versions(legacy: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.x.com"
        assert request.url.path == "/2/tweets/20"
        assert request.headers["authorization"] == "Bearer local-test-token"
        assert (
            request.url.params["expansions"]
            == "author_id,attachments.media_keys,referenced_posts,in_reply_to_user_id"
        )
        assert "note_post" in request.url.params["post.fields"]
        return httpx.Response(200, json=x_payload(legacy=legacy))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        post = await XApiProvider(client, "local-test-token").fetch("20")
    assert post.text == "Full, unmodified long-form post"
    assert post.quote and post.quote.id == "21"
    assert post.media[0].kind == "video"
    assert post.reposts == 3
    assert post.url == "https://x.com/fixture_author/status/20"


@pytest.mark.parametrize("change", ["mismatch", "private", "long", "author_missing", "malformed"])
async def test_x_rejects_unsafe_post(change: str) -> None:
    payload = x_payload()
    if change == "mismatch":
        payload["data"]["id"] = "25"
    elif change == "private":
        payload["includes"]["users"][0]["protected"] = True
    elif change == "long":
        payload["data"]["note_post"]["text"] = "a" * 12_001
    elif change == "author_missing":
        payload["includes"]["users"] = []
    else:
        payload["data"] = "invalid"
    async with client_for(payload) as client:
        with pytest.raises(UserError) as exc:
            await XApiProvider(client, "local-test-token").fetch("20")
    assert "local-test-token" not in str(exc.value)


async def test_x_unavailable_quote_does_not_fabricate_it() -> None:
    payload = x_payload()
    payload["includes"]["posts"] = []
    async with client_for(payload) as client:
        post = await XApiProvider(client, "local-test-token").fetch("20")
    assert post.quote is None
    assert any("quoted post is unavailable" in warning for warning in post.warnings)


async def test_x_error_body_is_not_echoed() -> None:
    async with client_for({"errors": [{"detail": "private-detail"}]}) as client:
        with pytest.raises(UserError) as exc:
            await XApiProvider(client, "local-test-token").fetch("20")
    assert "private-detail" not in str(exc.value)


def fx_reply_payloads() -> dict[str, dict[str, Any]]:
    """A reply (30) to a post (20) that itself replies to an earlier post and quotes another."""
    reply = fixture()
    reply["status"].update(
        {
            "id": "30",
            "text": "Replying with a photo",
            "replying_to": {"screen_name": "jack", "status": "20"},
            "media": fixture("fx_photo.json")["status"]["media"],
        }
    )
    reply["status"]["author"].update({"screen_name": "replier", "name": "Replier"})
    parent = fixture()
    parent["status"]["replying_to"] = {"screen_name": "someone", "status": "10"}
    parent["status"]["quote"] = copy.deepcopy(fixture("fx_photo.json")["status"])
    return {"30": reply, "20": parent}


def fx_client(payloads: dict[str, Any], requests: list[str]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.fxtwitter.com"
        post_id = request.url.path.rsplit("/", 1)[-1]
        requests.append(post_id)
        if post_id not in payloads:
            return httpx.Response(200, json={"code": 404, "message": "not found"})
        return httpx.Response(200, json=payloads[post_id])

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fx_reply_includes_parent_post_one_level() -> None:
    requests: list[str] = []
    async with fx_client(fx_reply_payloads(), requests) as client:
        post = await FxTwitterProvider(client).fetch("30")
    assert requests == ["30", "20"]
    assert post.parent is not None
    assert post.parent.id == "20" and post.parent.author_handle == "jack"
    assert post.parent.text == "just setting up my twttr"
    assert post.parent.parent is None and post.parent.quote is None
    # Single-parent replies no longer claim the conversation is missing.
    assert not any("conversation is not shown" in w for w in post.warnings)
    assert any("Earlier posts" in w for w in post.parent.warnings)
    assert any("quoted post is not shown" in w for w in post.parent.warnings)
    document = build_html(post, RenderAssets())
    assert document.count('aria-label="Replied-to post"') == 1
    assert "Replying to @jack" in document
    assert document.index("just setting up my twttr") < document.index("Replying with a photo")


async def test_fx_legacy_flat_reply_fields_are_supported() -> None:
    payloads = fx_reply_payloads()
    payloads["30"]["status"]["replying_to"] = "jack"
    payloads["30"]["status"]["replying_to_status"] = "20"
    requests: list[str] = []
    async with fx_client(payloads, requests) as client:
        post = await FxTwitterProvider(client).fetch("30")
    assert post.parent and post.parent.id == "20"


@pytest.mark.parametrize(
    "change", ["missing", "tombstone", "private", "mismatch", "bad_id", "self"]
)
async def test_fx_unavailable_parent_still_renders_reply(change: str) -> None:
    payloads = fx_reply_payloads()
    status = payloads["30"]["status"]
    if change == "missing":
        del payloads["20"]
    elif change == "tombstone":
        payloads["20"]["status"] = {"type": "tombstone"}
    elif change == "private":
        payloads["20"]["status"]["author"]["protected"] = True
    elif change == "mismatch":
        payloads["20"]["status"]["id"] = "21"
    elif change == "bad_id":
        status["replying_to"]["status"] = "../20"
    else:
        status["replying_to"]["status"] = "30"
    requests: list[str] = []
    async with fx_client(payloads, requests) as client:
        post = await FxTwitterProvider(client).fetch("30")
    assert post.id == "30" and post.parent is None
    assert any("replies to is unavailable" in w for w in post.warnings)
    assert requests.count("30") == 1
    if change in ("bad_id", "self"):
        assert requests == ["30"]


async def test_fx_parent_rate_limit_does_not_fail_reply() -> None:
    payloads = fx_reply_payloads()
    payloads["20"] = {"code": 429, "message": "slow down"}
    requests: list[str] = []
    async with fx_client(payloads, requests) as client:
        post = await FxTwitterProvider(client).fetch("30")
    assert post.parent is None
    assert any("replies to is unavailable" in w for w in post.warnings)


async def test_fx_reply_in_longer_thread_keeps_conversation_warning() -> None:
    payloads = fx_reply_payloads()
    payloads["30"]["thread"] = [{"id": "10"}]
    requests: list[str] = []
    async with fx_client(payloads, requests) as client:
        post = await FxTwitterProvider(client).fetch("30")
    assert post.parent is not None
    assert any("conversation is not shown" in w for w in post.warnings)


def x_reply_payload() -> dict[str, Any]:
    payload = x_payload()
    payload["data"]["referenced_posts"] = [
        {"type": "replied_to", "id": "19"},
        {"type": "quoted", "id": "21"},
    ]
    payload["data"]["in_reply_to_user_id"] = "13"
    payload["includes"]["users"].append(
        {"id": "13", "name": "Parent Author", "username": "parent_author", "protected": False}
    )
    payload["includes"]["posts"].append(
        {
            "id": "19",
            "author_id": "13",
            "text": "The original question",
            "referenced_posts": [{"type": "replied_to", "id": "18"}],
        }
    )
    return payload


async def test_x_reply_includes_parent_from_expansions() -> None:
    async with client_for(x_reply_payload()) as client:
        post = await XApiProvider(client, "local-test-token").fetch("20")
    assert post.parent and post.parent.id == "19"
    assert post.parent.author_handle == "parent_author"
    assert post.parent.text == "The original question"
    assert post.parent.parent is None
    assert post.quote and post.quote.id == "21"
    assert not any("conversation is not shown" in w for w in post.warnings)
    assert any("Earlier posts" in w for w in post.parent.warnings)


@pytest.mark.parametrize("change", ["missing_post", "missing_author", "private", "self"])
async def test_x_unavailable_parent_does_not_fabricate_it(change: str) -> None:
    payload = x_reply_payload()
    if change == "missing_post":
        payload["includes"]["posts"] = [p for p in payload["includes"]["posts"] if p["id"] != "19"]
    elif change == "missing_author":
        payload["includes"]["users"] = payload["includes"]["users"][:1]
    elif change == "private":
        payload["includes"]["users"][1]["protected"] = True
    else:
        payload["data"]["referenced_posts"][0]["id"] = "20"
    async with client_for(payload) as client:
        post = await XApiProvider(client, "local-test-token").fetch("20")
    assert post.id == "20" and post.parent is None
    assert any("replies to is unavailable" in w for w in post.warnings)
