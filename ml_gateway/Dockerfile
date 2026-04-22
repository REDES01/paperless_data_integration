# ML Gateway — FastAPI + TrOCR + mpnet
#
# Built from paperless_data_integration/:
#   docker build -f ml_gateway/Dockerfile -t ml_gateway:latest .
FROM python:3.12-slim

WORKDIR /app

# CPU torch from the dedicated index first, so the pip resolver doesn't try
# to pull in CUDA wheels when transformers is installed below.
RUN pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu \
    torch==2.4.1

COPY ml_gateway/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY ml_gateway/service.py /app/service.py

# Pre-download models at build time so the container doesn't need internet at run time
# and startup is fast (~5s instead of ~90s).
RUN python -c "\
from transformers import TrOCRProcessor, VisionEncoderDecoderModel;\
TrOCRProcessor.from_pretrained('microsoft/trocr-base-handwritten');\
VisionEncoderDecoderModel.from_pretrained('microsoft/trocr-base-handwritten');\
print('TrOCR cached')"

RUN python -c "\
from sentence_transformers import SentenceTransformer;\
SentenceTransformer('sentence-transformers/all-mpnet-base-v2');\
print('mpnet cached')"

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8000"]
