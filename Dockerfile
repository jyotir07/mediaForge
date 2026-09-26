FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dev deps are baked in so tests run inside the same image as the app (single image for a weekend MVP).
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

RUN useradd --create-home --uid 1000 mediaforge \
    && mkdir -p /data/media \
    && chown -R mediaforge:mediaforge /data/media /app

COPY --chown=mediaforge:mediaforge . .

USER mediaforge
EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000"]
