FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends poppler-utils && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Paths below are relative to the repo root — compose.yml sets
# `build.context: ..` so this Dockerfile sees the full paperless_data_integration
# directory, not just htr_consumer/. That lets us reuse region_slicer/ directly
# without duplicating files.

# Install deps first so we get a cache layer
COPY htr_consumer/requirements.txt /app/requirements.txt
COPY region_slicer/requirements.txt /app/region_slicer_requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt && \
    pip install --no-cache-dir -r /app/region_slicer_requirements.txt

# Vendor the slicer code so we don't duplicate the detection logic
COPY region_slicer/detector.py        /app/detector.py
COPY region_slicer/slicer.py          /app/slicer.py
COPY region_slicer/printed_filter.py  /app/printed_filter.py

# Consumer's own code
COPY htr_consumer/db.py          /app/db.py
COPY htr_consumer/processor.py   /app/processor.py
COPY htr_consumer/consumer.py    /app/consumer.py

ENTRYPOINT ["python", "consumer.py"]
