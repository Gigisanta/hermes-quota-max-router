FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN useradd --create-home --shell /usr/sbin/nologin router

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY core/ core/
COPY server/ server/
COPY dashboard/ dashboard/
COPY scripts/ scripts/
COPY config/ config/
COPY prompts/ prompts/
COPY registry/ registry/
COPY main.py pyproject.toml ./

RUN mkdir -p logs && chown -R router:router /app
USER router

ENV ROUTER_HTTP_HOST=0.0.0.0 \
    ROUTER_HTTP_PORT=8080 \
    ROUTER_LOG_FORMAT=json

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD python -c "import httpx, os; httpx.get(f'http://127.0.0.1:{os.environ[\"ROUTER_HTTP_PORT\"]}/v1/router/health', timeout=4).raise_for_status()"

CMD ["python", "-m", "server.app"]
