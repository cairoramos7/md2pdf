# =============================================================================
# md2pdf — Dockerfile (multi-stage production build)
# =============================================================================
# Stage 1: builder   → installs Python deps + Playwright Chromium
# Stage 2: runtime   → copies only what's needed into the final image
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1 — Builder (install everything, discard later)
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Build tools that will NOT end up in the final image
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Install Python deps into an isolated venv (easy to copy across stages)
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Download Chromium via Playwright (binary lands in ~/.cache/ms-playwright)
RUN playwright install chromium


# ---------------------------------------------------------------------------
# Stage 2 — Runtime (final image, minimal footprint)
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Playwright needs to know where the browser binary lives
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers

# Chromium runtime dependencies (no build tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libx11-xcb1 \
    # Fonts for proper PDF rendering
    fonts-liberation \
    fonts-noto-core \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# Copy venv with all Python deps from the builder stage
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy Chromium binary from the builder stage
COPY --from=builder /root/.cache/ms-playwright $PLAYWRIGHT_BROWSERS_PATH

WORKDIR /app

# Copy only the application files
COPY app.py .
COPY templates/ templates/

# Non-root user for security
RUN useradd --create-home appuser && \
    chown -R appuser:appuser /app $PLAYWRIGHT_BROWSERS_PATH
USER appuser

EXPOSE 8050

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8050/')" || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8050", "--workers", "2"]
