FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    DB_PATH=/app/data/hltv.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# База лежит на томе, чтобы переживать пересборку образа: в ней и состояние,
# и журнал отправленных событий — потеря журнала означала бы повторные
# уведомления обо всём подряд.
VOLUME ["/app/data"]

RUN useradd --create-home --uid 1000 app && mkdir -p /app/data && chown -R app /app
USER app

CMD ["python", "-m", "hltv_notify"]
