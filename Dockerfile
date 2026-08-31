FROM python:3.12-slim

# The base image carries whatever Debian had on the day IT was built, so a
# rebuild a week later still ships last week's openssl. Upgrading here is what
# actually closes the CVEs a scanner can do something about; the rest of what
# it reports (perl-base, ncurses, gzip, libsqlite3) has no Debian fix at all
# and no amount of rebasing removes it.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    DB_PATH=/app/data/hltv.db

WORKDIR /app

COPY requirements.txt .
# pip is a build-time tool: nothing at runtime imports it, and leaving it in
# means every future pip CVE shows up in a scan of an image that cannot use it.
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y pip

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
