# syntax=docker/dockerfile:1
FROM python:3.12-slim

LABEL org.opencontainers.image.title="flight-deals-scanner" \
      org.opencontainers.image.description="Multi-origin flight bargain scanner (Amadeus API)" \
      org.opencontainers.image.source="https://github.com/hypeitnow/flight-deals-scanner"

WORKDIR /app

# Copy package metadata and source first (cache-friendly layer order)
COPY pyproject.toml README.md requirements.txt ./
COPY flight_scanner.py ./
COPY data/ ./data/
COPY config.json ./

# Install the package — zero pip deps beyond setuptools
RUN pip install --no-cache-dir -e .

# prices.db lives here; mount a host directory to persist it across runs:
#   docker run -v $(pwd)/data:/app hypeitnow/flight-deals-scanner
VOLUME ["/app"]

ENTRYPOINT ["flight-scanner"]
# Default: offline demo — override with real args at runtime
CMD ["--demo"]
