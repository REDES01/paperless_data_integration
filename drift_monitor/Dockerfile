# Drift Monitor service — loads a pre-built alibi-detect MMDDriftOnline
# detector from MinIO at startup and exposes /drift/check, /health, /metrics.
#
# Built from paperless_data_integration/:
#   docker build -f drift_monitor/Dockerfile -t drift_monitor:latest .
FROM python:3.12-slim

WORKDIR /app

# Install torch CPU wheel explicitly so we don't pull 2GB of CUDA libs.
# This layer is by far the biggest (~600MB) — keep it first for cache reuse.
RUN pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu \
    torch==2.4.1

COPY drift_monitor/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY drift_monitor/service.py /app/service.py

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8000"]
