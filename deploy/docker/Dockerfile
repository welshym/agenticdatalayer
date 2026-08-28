# Business Context Layer (BCL) — all-in-one demo image.
# Runs all 9 FastAPI services in one container (matches the local start.sh model).
# Build context = repo root (agentic-data-layer/).
FROM python:3.12-slim

WORKDIR /app

# System deps: bash (entrypoint) + curl (health checks)
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps first (layer cache)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App source
COPY . /app

# Entrypoint launches all services
RUN chmod +x /app/deploy/docker/container-start.sh

# Agentic entry points exposed via the ALB; the rest are internal to the container
EXPOSE 8013 8018

# ACG UI health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://localhost:8013/ >/dev/null || exit 1

CMD ["/app/deploy/docker/container-start.sh"]
