FROM docker-hosted.hamainsurance.net/python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt

WORKDIR /app

# Install Internal Root CA
COPY certs/company-root-ca.crt \
    /usr/local/share/ca-certificates/company-root-ca.crt

RUN update-ca-certificates

# Install Python Packages From Internal Nexus
ARG PIP_INDEX_URL=https://nexus.hamainsurance.net/repository/py-proxy/simple

COPY requirements.txt ./

RUN python -m pip install \
    --no-cache-dir \
    --index-url "${PIP_INDEX_URL}" \
    -r requirements.txt

# Create Application User
RUN groupadd -g 10001 app \
    && useradd -u 10001 -g app app \
    && mkdir -p /data/uploads \
    && chown -R app:app /data/uploads

# Copy Application Files
COPY --chown=app:app main.py manager_excel.py backup_status.py import_employees.py bootstrap_admin.py prune_login_throttle.py ./
COPY --chown=app:app static ./static
COPY --chown=app:app templates ./templates
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app migrations ./migrations

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s \
    --timeout=5s \
    --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "127.0.0.1"]
