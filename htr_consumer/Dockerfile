FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends poppler-utils && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first so we get cache layer
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Vendor the slicer code so we don't duplicate the detection logic
# (build context is paperless_data_integration/; see compose.yml)
COPY region_slicer/detector.py   /app/detector.py
COPY region_slicer/slicer.py     /app/slicer.py

# Consumer's own code
COPY htr_consumer/db.py          /app/db.py
COPY htr_consumer/processor.py   /app/processor.py
COPY htr_consumer/consumer.py    /app/consumer.py

ENTRYPOINT ["python", "consumer.py"]
