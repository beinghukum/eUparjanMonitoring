# ── Build stage ────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app
COPY requirements.txt .

RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Runtime stage ──────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Non-root user for security
RUN useradd -m -u 1000 monitor
WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

COPY monitor.py .

# /tmp is writable and survives soft restarts (state persistence)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER monitor

EXPOSE 8080

CMD ["python", "-u", "monitor.py"]
