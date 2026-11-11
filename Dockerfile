FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/opt/browsers \
    XDG_CACHE_HOME=/tmp/.cache \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
RUN pip install --no-cache-dir uv==0.12.19
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project \
    && .venv/bin/playwright install --with-deps chromium \
    && apt-get update && apt-get install -y --no-install-recommends \
       fonts-noto-core fonts-noto-cjk fonts-noto-color-emoji ffmpeg \
    && rm -rf /var/lib/apt/lists/* /root/.cache
COPY src ./src
RUN uv sync --locked --no-dev \
    && useradd --uid 10001 --create-home bot \
    && chmod -R a+rX /app /opt/browsers
USER 10001:10001
CMD ["python", "-m", "xtoimage_bot"]
