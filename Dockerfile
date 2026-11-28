FROM python:3.12-slim

ARG CA_CERTS=""
RUN if [ -n "$CA_CERTS" ]; then \
      cp "$CA_CERTS" /usr/local/share/ca-certificates/company.crt && \
      update-ca-certificates; \
    fi

WORKDIR /app

RUN adduser --disabled-password --gecos '' appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY attendance/ ./attendance/
COPY migrations/ ./migrations/
COPY alembic.ini .

USER appuser

HEALTHCHECK --interval=15s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/ready')"

CMD ["uvicorn", "attendance.main:app", "--host", "0.0.0.0", "--port", "8000"]
