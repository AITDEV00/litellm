FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir kubernetes httpx aiohttp

COPY controller/ ./controller/
COPY hooks/ ./hooks/

CMD ["python", "-m", "controller"]
