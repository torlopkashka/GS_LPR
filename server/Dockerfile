FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=Europe/Moscow \
    LPR_CONFIG=/config/config.yaml LPR_DATA_DIR=/data \
    OMP_NUM_THREADS=1 HOME=/data/home

RUN apt-get update && apt-get install -y --no-install-recommends tzdata libglib2.0-0 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

# Предзагрузка моделей в образ, чтобы сервер стартовал без доступа к GitHub
ARG DETECTOR=yolo-v9-t-384-license-plate-end2end
ARG OCR=cct-xs-v2-global-model
RUN HOME=/root python -c "from fast_alpr import ALPR; ALPR(detector_model='$DETECTOR', ocr_model='$OCR')" \
    && mkdir -p /data/home && cp -r /root/.cache /opt/model-cache

COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fs http://127.0.0.1:8000/healthz || exit 1
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
