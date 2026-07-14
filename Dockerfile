# Slim Python base — small image, no build toolchain baggage.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install the package (into site-packages) from source. Copying only the build
# inputs keeps this layer cached until they actually change.
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Run as a non-root user. /data is the runtime working directory: the mounted
# config.yaml is read from here and the SQLite DB (./uptime.db) is written here,
# so mounting a volume at /data persists the database across restarts. A fresh
# named volume inherits /data's ownership from the image, so `app` can write it.
RUN useradd --create-home --uid 1000 app \
    && mkdir /data && chown app:app /data \
    && rm -rf /src
USER app
WORKDIR /data

EXPOSE 8000

# Bind to 0.0.0.0 (not the 127.0.0.1 default) so the published port is
# reachable from outside the container.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/status')" || exit 1

CMD ["pulsewatch", "serve", "--host", "0.0.0.0", "--port", "8000"]
