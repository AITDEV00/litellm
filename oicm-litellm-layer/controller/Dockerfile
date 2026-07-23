FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir kubernetes httpx aiohttp

COPY controller/ ./controller/
COPY hooks/ ./hooks/
COPY model_prices_and_context_window.json /app/model_prices_and_context_window.json

CMD ["python", "-m", "controller"]
