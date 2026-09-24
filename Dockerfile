FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LISTINGWATCHER_CONFIG=/app/config.yaml \
    LISTINGWATCHER_DB=/data/listingwatcher.sqlite \
    LISTINGWATCHER_LIVE_CONFIG=/data/config.yaml \
    TZ=Europe/Paris

RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY listingwatcher ./listingwatcher
COPY config.yaml .

RUN mkdir -p /data && useradd -u 1000 -M -s /usr/sbin/nologin listingwatcher && chown listingwatcher /data
USER listingwatcher
VOLUME ["/data"]
EXPOSE 8080

ENTRYPOINT ["python", "-m", "listingwatcher"]
CMD ["run"]
