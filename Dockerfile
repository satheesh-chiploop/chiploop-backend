FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CHIPLOOP_PRIVATE_ARTIFACT_ROOT=/var/lib/chiploop/artifacts

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    build-essential \
    ca-certificates \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend
COPY requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/backend
RUN useradd --create-home --uid 10001 chiploop \
    && mkdir -p /app/backend/backend/workflows /var/lib/chiploop/artifacts /var/lib/chiploop/tmp \
    && chown -R chiploop:chiploop /app/backend /var/lib/chiploop

USER chiploop
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
