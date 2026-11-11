# X to Image · Telegram Bot

Paste a public X/Twitter post link into Telegram. Get a clean, attributed image card with its text, author, photos, quoted post and, for replies, the post being replied to. Self-hosted, with light/dark themes and original PNG downloads.

An independent implementation inspired by the simple **link → image** experience of xtoimage.com. It does not call that site, copy its code/assets, or create a public screenshot gallery. It is not affiliated with X, Telegram, FxEmbed or xtoimage.com.

## What works

- Public `x.com` and `twitter.com` status links, including mobile and `/i/status/` variants
- Author name, handle, avatar, full supplied post text, timestamp and available engagement counts
- Up to four photos or video thumbnails, plus one quoted post
- Replies include the post they reply to, shown above the reply with a conversation line
- `/theme light` and `/theme dark`; `/file <link>` sends an uncompressed PNG
- Clear responses for unavailable/protected posts, provider failures, oversized content and rate limits
- Optional numeric Telegram user allowlist; private chats only by default
- Bounded concurrency, timeouts and media sizes; no database or screenshot history

Videos are represented by still thumbnails. Longer threads (beyond the direct parent), polls, articles, cards and deeper quote chains are not fully reproduced; missing content is explicitly noted. Posts above the 12,000-character safety limit or the renderer’s maximum height are rejected rather than silently cropped. Generated images are snapshots, not live embeds.

## Quick start with Docker

1. Create a bot using Telegram’s official [@BotFather](https://t.me/BotFather). Keep its token private. Never paste it into an issue, repository, screenshot or chat with an assistant.
2. Clone this repository and copy `.env.example` to `.env`.
3. Enter `TELEGRAM_BOT_TOKEN` in that local file. Keep `POST_PROVIDER=fxtwitter` for the no-key third-party provider, or choose the official X provider below.
4. Set `ALLOWED_USER_IDS` to your numeric Telegram ID. If you don’t know it, start the bot, send `/id`, then add that ID and restart. An empty allowlist permits any user who discovers the bot to use it in private chats.
5. Run:

```sh
chmod 600 .env
docker compose up --build -d
docker compose logs --tail=50 -f bot
```

Open your bot in Telegram, tap Start, and send a public post link. Stop with `docker compose down`. Run exactly one polling instance for each bot token. No inbound port, webhook, public domain or paid hosting subscription is required, but the process needs a computer/server that stays on.

**Existing webhook?** The app refuses to replace it. Remove it intentionally or use a separate bot. It does not discard pending updates at startup.

### Commands

| Command | Result |
| --- | --- |
| a post URL, or `/image <URL>` | Shareable photo preview |
| `/file <URL>` | Original PNG document |
| `/theme light` / `/theme dark` | Your style until the process restarts |
| `/id` | Your numeric Telegram user ID |
| `/privacy` | Data flow and provider disclosure |
| `/help` | Usage and limits |

Use `/file` when exact quality matters: Telegram can recompress photos. The original-post button links back to X.

## Choose a post provider

### FxTwitter / FxEmbed (default)

`POST_PROVIDER=fxtwitter` uses the documented [FxEmbed v2 status API](https://docs.fxembed.com/api/twitter/operations/2statusid/). No X credential is required. It sends the post ID to an independent third-party service, then downloads supported images from `pbs.twimg.com`. For a reply, it makes a second request for the replied-to post; if that fails, the reply still renders and the caption notes it. Cards use the provider's display text rather than raw text, so hidden photo/video and quote attachment links are not printed as extra `t.co` URLs.

This is an **unofficial dependency**, with its own policies, rate limits and availability. It may break or return incomplete information when X changes. This project does not log in to X, scrape protected pages, bypass CAPTCHAs or evade access restrictions. Evaluate the provider and relevant terms for your intended use. Its open-source license does not grant rights to other people’s posts.

### Official X API

```dotenv
POST_PROVIDER=x_api
X_BEARER_TOKEN=your-token-kept-private
```

Use your own [X developer access](https://docs.x.com/x-api/introduction), current plan/credits and permissions. The adapter uses the current X OpenAPI `post.fields` schema and also normalizes older tweet-shaped response data. Replied-to posts come from the same request's `referenced_posts` expansion, so replies cost no extra API call. No X account, paid access or key is provisioned by this project. The adapter is fixture-tested; a real authenticated X request must be verified with your own account before production use. There is no automatic fallback from X to a third party.

## Native Python setup

Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/) are required.

```sh
uv sync --locked --group dev
uv run playwright install --with-deps chromium
# Export TELEGRAM_BOT_TOKEN and optional settings securely in your shell/service.
# The Python entrypoint does not automatically read .env; Compose does.
uv run xtoimage-bot
```

For emoji/CJK on Debian/Ubuntu, install `fonts-noto-color-emoji` and `fonts-noto-cjk`. Other scripts depend on installed fonts. A system browser can be selected with `CHROMIUM_EXECUTABLE_PATH`. Native installs should run under a dedicated unprivileged service account. Proxy settings such as `HTTPS_PROXY`/`ALL_PROXY` are honored for API traffic; use only proxies you trust with your bot credentials.

## Configuration

See [`.env.example`](.env.example). Tokens can also be read from `TELEGRAM_BOT_TOKEN_FILE` and `X_BEARER_TOKEN_FILE` for an operator-managed secret mount. Do not set both a secret and its `_FILE` option. File paths are container-local; add a read-only Compose mount when using this option. Never bake secrets into the Docker image or commit `.env`.

The polling application processes one update at a time, providing real backpressure instead of spawning an unbounded task backlog. Default limits: one active conversion, ten-second user cooldown, 32 queued Telegram updates, 90-second conversion deadline, 20-second outbound request deadline, 2 MB provider JSON, 8 MB per source image, 16 megapixels per decoded source image. The reusable renderer/admission components support up to four concurrent jobs via MAX_CONCURRENCY, but this does not increase the polling application’s sequential throughput. Commands wait behind an active conversion.

## Security and privacy

- Submitted URLs are parsed, never visited. Only fixed API hosts and `pbs.twimg.com` are requested; redirects and arbitrary media hosts are rejected
- Images are decoded, size-checked and re-encoded as PNG, stripping metadata before rendering
- HTML text is escaped; JavaScript, external browser networking and service workers are disabled. Only validated inline PNGs are rendered
- Chromium uses Playwright’s default launch settings. The browser’s OS sandbox is not a security boundary here; use the supplied unprivileged, read-only, capability-dropped container and keep dependencies patched
- The bot retains bounded cooldown/theme state in memory. It creates no persistent post/image archive or public gallery. Chromium may use temporary profile/cache files under `/tmp`, removed on context/process cleanup; the supplied container uses ephemeral tmpfs
- Logs intentionally exclude tokens, request URLs, message content and raw exceptions. Provider and Telegram retention policies are separate
- Your post ID reaches the selected provider; rendered content reaches Telegram and the chat recipients. Group use is opt-in via `PRIVATE_CHATS_ONLY=false`; keep Telegram’s bot privacy mode enabled unless you deliberately need broader access

Only convert and redistribute content you have permission to use. Preserve source attribution. See [X’s display requirements](https://docs.x.com/developer-terms/display-requirements), [developer guidelines](https://docs.x.com/developer-guidelines) and [terms](https://x.com/en/tos). A static exported image cannot update itself after edits/deletion, so this tool does not promise compliance with every live-display or removal obligation. Operators are responsible for their usage and for handling removal requests, including copies shared to Telegram.

## Development and tests

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run python scripts/render_demo.py
```

Tests cover URL/SSRF boundaries, redirects and payload limits, normalized Fx/X fixtures, unavailable quotes and reply parents, escaped HTML, actual Chromium rendering, and a real python-telegram-bot update pipeline backed by a fake Telegram transport. No credential is required and CI makes no live X/Telegram requests. Browser tests require Chromium; install it before running the full suite.

CI also builds and smoke-tests the locked Docker image and saves synthetic image previews as a short-lived Actions artifact. Public provider availability is intentionally a separate manual smoke check, not a flaky CI dependency.

## Project map

- `src/xtoimage_bot/bot.py`: commands, access checks, rate control and delivery
- `providers.py`: FxEmbed and official X adapters
- `urls.py`, `network.py`, `media.py`: input and outbound resource boundaries
- `render.py`: isolated HTML-to-PNG renderer
- `tests/`: offline fixtures and integration tests
- `Dockerfile`, `compose.yaml`: self-hosting

No Telegram bot token or hosting service is included. Source implementation and tests can be used immediately; running a live bot requires your privately configured token and an always-on host.
