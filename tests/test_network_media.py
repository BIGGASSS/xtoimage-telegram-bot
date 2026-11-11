from io import BytesIO

import httpx
import pytest
from PIL import Image

from xtoimage_bot.media import MediaLoader, sanitize_image
from xtoimage_bot.models import Media, Post, UserError
from xtoimage_bot.network import API_HOSTS, fetch_bytes, fetch_json, validate_remote_url


@pytest.mark.parametrize(
    "url",
    [
        "http://pbs.twimg.com/a.jpg",
        "https://pbs.twimg.com.evil/a",
        "https://pbs.twimg.com:443/a",
        "https://a@pbs.twimg.com/a",
        "https://localhost/a",
        "https://[::1]/a",
    ],
)
def test_media_ssrf_rejected(url):
    with pytest.raises(UserError):
        validate_remote_url(url, frozenset({"pbs.twimg.com"}))


@pytest.mark.parametrize("status", [301, 302, 307, 401, 403, 404, 429, 500])
async def test_bad_status_and_redirects_never_followed(status):
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(status, headers={"location": "http://127.0.0.1/secret"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UserError):
            await fetch_json(client, "https://api.fxtwitter.com/2/status/20")
    assert len(requests) == 1


async def test_json_invalid_and_oversized():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"not json"))
    ) as client:
        with pytest.raises(UserError, match="unreadable"):
            await fetch_json(client, "https://api.x.com/2/tweets/20")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 101))
    ) as client:
        with pytest.raises(UserError, match="large"):
            await fetch_bytes(client, "https://api.x.com/2/tweets/20", hosts=API_HOSTS, limit=100)


async def test_network_error_redacts_url():
    def handler(request):
        raise httpx.ConnectError("SECRET-TOKEN https://private.example", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UserError) as error:
            await fetch_json(client, "https://api.x.com/2/tweets/20")
        assert "SECRET" not in str(error.value)


def test_sanitize_png_removes_metadata_and_resizes():
    source = BytesIO()
    Image.new("RGB", (2000, 1000), "coral").save(source, format="PNG")
    result = sanitize_image(source.getvalue())
    assert result.startswith("data:image/png;base64,")
    with pytest.raises(UserError):
        sanitize_image(b'<svg onload="alert(1)">')


async def test_partial_media_failure_preserves_slots():
    source = BytesIO()
    Image.new("RGB", (10, 10), "coral").save(source, format="PNG")

    def handler(request):
        return httpx.Response(200, content=source.getvalue())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        post = Post(
            "20",
            "https://x.com/jack/status/20",
            "Jack",
            "jack",
            "Hi",
            media=(Media("https://evil.org/one"), Media("https://pbs.twimg.com/two")),
        )
        assets = await MediaLoader(client).load(post)
        assert assets.media[0] == "" and assets.media[1].startswith("data:image/png;")
        assert len(assets.warnings) == 1


async def test_parent_media_loaded_into_parent_slots():
    source = BytesIO()
    Image.new("RGB", (10, 10), "coral").save(source, format="PNG")
    requested = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(200, content=source.getvalue())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        parent = Post(
            "19",
            "https://x.com/a/status/19",
            "A",
            "a",
            "Parent",
            avatar_url="https://pbs.twimg.com/parent-avatar",
            media=(Media("https://pbs.twimg.com/parent-one"), Media("https://evil.org/x")),
        )
        post = Post("20", "https://x.com/jack/status/20", "Jack", "jack", "Hi", parent=parent)
        assets = await MediaLoader(client).load(post)
    assert assets.parent_avatar.startswith("data:image/png;")
    assert assets.parent_media[0].startswith("data:image/png;") and assets.parent_media[1] == ""
    assert assets.media == () and assets.quote_media == ()
    assert "/parent-one" in requested and len(assets.warnings) == 1
