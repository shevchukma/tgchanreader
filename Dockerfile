FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    TZ=Europe/Moscow

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY tgchanreader ./tgchanreader

RUN mkdir -p /data

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os,sys,urllib.request; sys.exit(0) if os.environ.get('WEB_ENABLED','true').lower() in ('0','false','no','off') else urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('WEB_PORT','8080'), timeout=4)" || exit 1

CMD ["python", "-m", "tgchanreader"]
