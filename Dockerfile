FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY serve.py healthcheck.py ./

# -u = unbuffered, supaya print/logging langsung muncul di `docker compose logs`
CMD ["python", "-u", "serve.py"]
