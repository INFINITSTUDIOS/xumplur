# PLÜR Scene Dashboard — container for Kinsta Application Hosting (or any Docker host)
FROM python:3.12-slim

# ffmpeg is required for thumbnails, filmstrips, and film rendering
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Cloud runtime settings (override in the host's env panel as needed)
ENV HOST=0.0.0.0
ENV PORT=8080
ENV DATA_DIR=/data
# HF_KEY, DASH_USER, DASH_PASSWORD are provided as host secrets — never baked into the image.

EXPOSE 8080
CMD ["python3", "app.py"]
