# syntax=docker/dockerfile:1.7
# One image, several roles (pipeline, demo API, KPI API, dashboard): the command decides.

FROM python:3.12-slim-bookworm AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.lock .
RUN pip install --require-hashes -r requirements.lock
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps .

FROM python:3.12-slim-bookworm AS runtime
LABEL org.opencontainers.image.title="retail-data-platform" \
      org.opencontainers.image.description="Retail ETL, PostgreSQL star schema, FastAPI and Streamlit KPIs"
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    LOG_FORMAT=json \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_SERVER_HEADLESS=true
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /home/app --create-home app \
    && mkdir -p /data && chown app:app /data
COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app src/retail_platform/dashboard/app.py /app/dashboard.py
WORKDIR /app
USER app
CMD ["retail", "--help"]
