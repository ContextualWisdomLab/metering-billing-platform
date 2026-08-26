# Runtime image for `python -m metering_billing.http_app`.
# Single stage on purpose: the app is stdlib plus one hash-locked wheel.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

# Install only the hash-locked runtime dependency set, mirroring CI flags so
# the image can never drift from the lockfile.
COPY requirements-runtime.txt ./requirements-runtime.txt
RUN python -m pip install \
        --disable-pip-version-check \
        --only-binary=:all: \
        --require-hashes \
        -r requirements-runtime.txt

COPY metering_billing ./metering_billing
COPY schemas ./schemas
COPY scripts ./scripts
COPY database/migrations ./database/migrations
COPY compose/k6/seed.py ./compose/k6/seed.py

RUN useradd --uid 10001 --user-group --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

# main() honors PORT and defaults to 8000.
ENV PORT=8000
EXPOSE 8000

CMD ["python", "-m", "metering_billing.http_app"]
