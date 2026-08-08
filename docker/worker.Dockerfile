FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /worker
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY apps/api/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "yt-dlp==2025.6.30" \
    && addgroup --system worker \
    && adduser --system --ingroup worker worker
COPY --chown=worker:worker apps/api/app ./app

USER worker

CMD ["celery", "-A", "app.workers.celery_app:celery_app", "worker", "--loglevel=INFO"]