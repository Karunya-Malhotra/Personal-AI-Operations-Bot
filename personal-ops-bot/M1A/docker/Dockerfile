# One image, several entrypoints (api, cli, and from M2 worker). Same code,
# same dependencies, same migrations -- so "works in the api but not the
# worker" is not a class of bug that can exist.
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

FROM base AS builder
WORKDIR /build
RUN pip install --no-cache-dir hatchling
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir --prefix=/install .

FROM base AS runtime
# Non-root by default. The fetcher container (M6) depends on this being the
# habit rather than a special case.
RUN useradd --create-home --uid 10001 aiops
COPY --from=builder /install /usr/local
WORKDIR /srv
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
USER aiops
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
