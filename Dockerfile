FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    LISTEN_PORT=8080

WORKDIR /app
COPY pyproject.toml README.md ./
COPY agent_helper ./agent_helper
RUN pip install --no-cache-dir . \
    && useradd --system --uid 10001 --no-create-home app \
    && mkdir -p /data && chown app /data

USER app
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"LISTEN_PORT\"]}/healthz', timeout=2)"

# --no-access-log: uvicorn's access log would record client addresses (docs/decisions/0008).
# --no-proxy-headers: forwarded headers are handled by TRUST_PROXY_HEADERS in the app only.
CMD ["sh", "-c", "exec uvicorn agent_helper.app:create_app --factory --host 0.0.0.0 --port ${LISTEN_PORT} --no-access-log --no-proxy-headers"]
