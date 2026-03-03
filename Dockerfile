FROM python:3.12-alpine

# Install build deps needed by qbittorrent-api, then clean up in one layer
RUN apk add --no-cache --virtual .build-deps gcc musl-dev \
    && pip install --no-cache-dir qbittorrent-api==2024.2.59 \
    && apk del .build-deps

WORKDIR /app
COPY cleaner.py .

CMD ["python", "-u", "cleaner.py"]
