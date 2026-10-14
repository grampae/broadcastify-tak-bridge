FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY feed_manager.py geo.py dedup.py cot_sender.py build_address_db.py ./
COPY feeds/ feeds/
COPY dashboard/ dashboard/
COPY locations_*.yml ./

CMD ["python3", "-u", "feed_manager.py", "--config", "/app/feeds.yml"]
