# ════════════════════════════════════════════════════════
#  Claude Incognito Telegram Bot — Dockerfile
#  Multi-stage, non-root, minimal Alpine image
# ════════════════════════════════════════════════════════

# ── Stage 1: Build ───────────────────────────────────────
FROM python:3.12-alpine AS builder

WORKDIR /build

RUN apk add --no-cache gcc musl-dev libffi-dev

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: Runtime ─────────────────────────────────────
FROM python:3.12-alpine AS runtime

LABEL maintainer="Claude Incognito Bot"
LABEL description="Telegram bot for Claude AI — incognito mode"
LABEL version="1.0.0"

# Non-root user
RUN addgroup -S botgroup && adduser -S botuser -G botgroup

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy bot files
COPY --chown=botuser:botgroup bot.py        .
COPY --chown=botuser:botgroup healthcheck.py .

# Persistent logs directory
RUN mkdir -p /app/logs && chown botuser:botgroup /app/logs

USER botuser

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python healthcheck.py

CMD ["python", "-u", "bot.py"]
