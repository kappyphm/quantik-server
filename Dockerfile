FROM python:3.13-slim-bookworm AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY backend/pyproject.toml ./backend/pyproject.toml
COPY backend/src ./backend/src
RUN python -m venv /venv && /venv/bin/pip install --no-cache-dir './backend[broker,ops,market,quant]' --extra-index-url https://vnstocks.com/api/packages

FROM python:3.13-slim-bookworm AS runtime
ENV PATH=/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/app/runtime
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 ca-certificates && rm -rf /var/lib/apt/lists/* && groupadd --gid 10001 quantik && useradd --uid 10001 --gid 10001 --home-dir /app/runtime quantik
WORKDIR /app
COPY --from=build /venv /venv
RUN /venv/bin/python -m pip check && /venv/bin/python -m pip uninstall -y pip && /usr/local/bin/python -m pip uninstall -y pip
COPY backend/alembic.ini ./backend/alembic.ini
COPY backend/migrations ./backend/migrations
RUN mkdir -p runtime && chown 10001:10001 runtime
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "service.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-access-log"]
