FROM node:24.20.0-bookworm-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS frontend

WORKDIR /build
RUN npm install --global --ignore-scripts npm@12.0.2 \
    && npm install --prefix /tmp/npm-security-patches --no-save \
      --ignore-scripts --no-audit \
      brace-expansion@5.0.9 ip-address@10.3.1 tar@7.5.21 \
    && npm_modules="$(npm root --global)/npm/node_modules" \
    && rm -rf \
      "$npm_modules/brace-expansion" \
      "$npm_modules/ip-address" \
      "$npm_modules/tar" \
    && cp -a /tmp/npm-security-patches/node_modules/brace-expansion "$npm_modules/" \
    && cp -a /tmp/npm-security-patches/node_modules/ip-address "$npm_modules/" \
    && cp -a /tmp/npm-security-patches/node_modules/tar "$npm_modules/" \
    && rm -rf /tmp/npm-security-patches \
    && npm --version
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY vite.config.mjs ./
COPY scripts/finalize-assets.mjs ./scripts/finalize-assets.mjs
COPY app/static ./app/static
COPY app/templates ./app/templates
RUN npm run build

FROM python:3.12.14-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY requirements.lock ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --require-hashes -r requirements.lock

FROM python:3.12.14-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates libmagic1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
RUN playwright install --with-deps chromium

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app /data/storage /data/backup-temp \
    && chown -R app:app /app /data

WORKDIR /app
COPY --chown=app:app app ./app
COPY --from=frontend --chown=app:app /build/app/static/dist ./app/static/dist
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app alembic.ini ./alembic.ini
COPY --chown=app:app LICENSE THIRD_PARTY_NOTICES.md ./
COPY --chown=app:app docs/DEPENDENCY_LICENSES.md ./docs/DEPENDENCY_LICENSES.md
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl --fail http://127.0.0.1:8000/health/live || exit 1

CMD ["sh", "scripts/start.sh"]
