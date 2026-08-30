FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    DB_PATH=/app/data/hltv.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# The database sits on a volume so it survives an image rebuild: it holds both
# the state and the journal of sent events — losing the journal would mean
# repeat notifications about everything.
VOLUME ["/app/data"]

RUN useradd --create-home --uid 1000 app && mkdir -p /app/data && chown -R app /app
USER app

# We check not "the process is alive" but "polling is happening": a hung
# process looks healthy to Docker and the restart policy would leave it alone.
HEALTHCHECK --interval=5m --timeout=30s --start-period=2m --retries=3     CMD python -m hltv_notify --health || exit 1

CMD ["python", "-m", "hltv_notify"]
