# Base image pinned by digest (multi-arch index of python:3.12-slim). Update deliberately, see docs/operations.md.
FROM python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    LISTEN_PORT=8080

WORKDIR /app
# Exact versions with hashes; pip refuses anything that does not match.
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.lock \
    && useradd --system --uid 10001 --no-create-home app \
    && mkdir -p /data && chown app /data
# The package is run from source (uvicorn puts /app on the import path), so no build tooling is fetched.
COPY agent_helper ./agent_helper

USER app
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"LISTEN_PORT\"]}/healthz', timeout=2)"

# Serves LISTEN_PORT, and ADMIN_PORT if set (only /admin), without uvicorn's access log (it would record
# client addresses, docs/decisions/0008) and without uvicorn's proxy-header handling.
CMD ["python", "-m", "agent_helper.serve"]
