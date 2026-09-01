FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY readyscript-orders ./readyscript-orders
COPY certs/russian_trusted_root_ca.crt /usr/local/share/ca-certificates/russian_trusted_root_ca.crt

RUN update-ca-certificates && pip install --no-cache-dir .

CMD ["michelangelo-telegram-bot"]
