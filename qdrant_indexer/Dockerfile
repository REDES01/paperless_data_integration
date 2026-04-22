# Qdrant indexer — listens on paperless.uploads, chunks + embeds merged_text,
# upserts to Qdrant. Shares the mpnet model with ml-gateway.
#
# Built from paperless_data_integration/:
#   docker build -f qdrant_indexer/Dockerfile -t qdrant_indexer:latest .
FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu \
    torch==2.4.1

COPY qdrant_indexer/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY qdrant_indexer/indexer.py /app/indexer.py

# Pre-download mpnet so first event is processed without a 90s stall.
RUN python -c "\
from sentence_transformers import SentenceTransformer;\
SentenceTransformer('sentence-transformers/all-mpnet-base-v2');\
print('mpnet cached')"

ENV PYTHONUNBUFFERED=1
CMD ["python", "/app/indexer.py"]
