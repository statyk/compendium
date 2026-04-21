# syntax=docker/dockerfile:1.7

FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_INSTALL_DIR=/usr/local/bin \
    PATH="/usr/local/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE alembic.ini ./
COPY src ./src
COPY migrations ./migrations

RUN uv sync --frozen --no-dev --extra postgres


FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}" \
    COMPENDIUM_COVER_CACHE_DIR=/var/cache/compendium/covers

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1000 compendium \
    && useradd --system --uid 1000 --gid compendium --home /app compendium \
    && mkdir -p /var/cache/compendium/covers \
    && chown -R compendium:compendium /var/cache/compendium

WORKDIR /app

COPY --from=builder --chown=compendium:compendium /app /app
COPY --chown=compendium:compendium docker/compendium/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER compendium

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
