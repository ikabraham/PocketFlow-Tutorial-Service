FROM python:3.11-slim

# git is needed at runtime: main.py clones the repo being analyzed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir fastapi uvicorn redis

COPY . .

RUN useradd -m -u 1000 app && mkdir -p /app/output /app/data && chown -R app /app
USER app

ENV OUTPUT_ROOT=/app/output \
    REGISTRY_FILE=/app/data/jobs.json

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]

