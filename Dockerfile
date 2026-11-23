# syntax=docker/dockerfile:1

# The React UI (gab-25/cardledger_ui), built from the `ui` git submodule at the commit it is pinned to:
# check it out first (`git submodule update --init`, or actions/checkout with `submodules: true`).
FROM node:26-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build

# The Python dependencies, installed into /opt/venv. git is needed here only, for the git-sourced cardledger-vision
# (uv.lock pins its commit), and stays out of the final image.
FROM python:3.14-slim AS deps

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DJANGO_DEBUG=false

WORKDIR /app

COPY --from=deps /opt/venv /opt/venv

COPY --exclude=ui . .

COPY --from=ui /ui/dist /app/ui/dist

RUN python manage.py collectstatic --noinput

RUN useradd --system --create-home --uid 1000 app
USER app
EXPOSE 8000

# Apply pending migrations before serving (the Celery worker overrides CMD and skips this).
CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers ${GUNICORN_WORKERS:-2}"]
