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

# Проверяем не «процесс жив», а «опрос идёт»: зависший процесс для докера
# выглядит здоровым, и restart-policy его бы не тронула.
HEALTHCHECK --interval=5m --timeout=30s --start-period=2m --retries=3     CMD python -m hltv_notify --health || exit 1

CMD ["python", "-m", "hltv_notify"]
