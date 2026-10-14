# =============================================================================
# Stage 1: Builder - compile and install runtime dependencies
# =============================================================================
FROM python:3.12-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Install build dependencies if needed for wheel compilation
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# =============================================================================
# Stage 2: Test - dedicated stage for running linting & tests in CI/Docker
# =============================================================================
FROM builder AS test

WORKDIR /app
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY . .
CMD ["pytest", "-v"]

# =============================================================================
# Stage 3: Runner - lean, non-root production runtime
# =============================================================================
FROM python:3.12-slim AS runner

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ENV=production

WORKDIR /app

# Copy installed runtime dependencies from builder (no compilers present in runner)
COPY --from=builder /install /usr/local

# Create non-root system user and group (uid 10001)
RUN groupadd -g 10001 aurik && \
    useradd -u 10001 -g aurik -s /bin/bash -m aurik

# Copy application source code and migrations
COPY app/ ./app/
COPY alembic.ini .
COPY alembic/ ./alembic/

# Note: The CLI seed command reads reference metadata, historical data, and vendor samples
# directly from fixtures/ to populate the database.
COPY fixtures/ ./fixtures/

# Ensure all application files are owned by the non-root user
RUN chown -R aurik:aurik /app

USER aurik

EXPOSE 8000

# Native Python healthcheck (requires no external binaries like curl or wget)
HEALTHCHECK --interval=15s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').getcode() == 200 else 1)"

# Exec-form default entrypoint (SIGTERM reaches process directly)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
