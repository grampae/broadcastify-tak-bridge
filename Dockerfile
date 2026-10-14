FROM python:3.11-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir --no-deps .

ENTRYPOINT ["broadcastify-tak-bridge"]
CMD ["--config", "/app/config.yml"]
