FROM python:3.12-slim

WORKDIR /app

# Build-time network knobs (Clash TUN / corporate proxy):
#   docker compose build --build-arg HTTP_PROXY=http://127.0.0.1:7890 ...
#   INSTALL_SYSTEM_OCR=0  → skip apt OCR pkgs (local TUN escape hatch; PDF OCR degraded)
# Production: build in CI / clean egress, push image; runtime never runs apt.
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG http_proxy
ARG https_proxy
ARG NO_PROXY
ARG no_proxy
ARG INSTALL_SYSTEM_OCR=1

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home /app --shell /usr/sbin/nologin app

# OCR system deps are optional at build time. Default ON for full/production images.
RUN if [ "$INSTALL_SYSTEM_OCR" = "1" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends \
           tesseract-ocr tesseract-ocr-chi-sim poppler-utils \
      && rm -rf /var/lib/apt/lists/*; \
    else \
      echo "INSTALL_SYSTEM_OCR=0: skipping tesseract/poppler (text/markdown ingest still works)"; \
    fi

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/uploads /app/data \
    && chown -R app:app /app

USER app

EXPOSE 8080

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]
