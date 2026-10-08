# Playwright sürümü requirements.txt ile aynı olmalı (imajdaki tarayıcılar o sürüme göre gelir)
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Istanbul \
    DATA_DIR=/data \
    HEADLESS=true

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY ekampus ./ekampus
RUN mkdir -p /data && chown pwuser:pwuser /data
USER pwuser
VOLUME ["/data"]

HEALTHCHECK --interval=5m --timeout=20s --start-period=3m --retries=2 \
    CMD ["python", "-m", "ekampus", "health"]

CMD ["python", "-m", "ekampus", "bot"]
