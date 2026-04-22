# HTR trainer — fine-tunes TrOCR, logs to MLflow.
#
# Built from paperless_data_integration/:
#   docker build -f training/Dockerfile -t htr_trainer:latest .
#
# Usage (one candidate):
#   docker run --rm --network paperless_ml_net \
#     -e MLFLOW_TRACKING_URI=http://mlflow:5000 \
#     -e MINIO_ENDPOINT=minio:9000 \
#     -e MINIO_ACCESS_KEY=admin \
#     -e MINIO_SECRET_KEY=paperless_minio \
#     htr_trainer:latest --config configs/baseline.yaml

FROM python:3.12-slim

WORKDIR /app

# CPU-only torch from pytorch's dedicated index (smaller than pypi wheel)
RUN pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu \
    torch==2.4.1

COPY training/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Pre-download the base TrOCR model into the image so training runs don't
# pay the ~600 MB download cost each time. mpnet not needed — we don't do
# retrieval training.
RUN python -c "\
from transformers import TrOCRProcessor, VisionEncoderDecoderModel;\
TrOCRProcessor.from_pretrained('microsoft/trocr-base-handwritten');\
VisionEncoderDecoderModel.from_pretrained('microsoft/trocr-base-handwritten');\
print('TrOCR cached')"

COPY training/ /app/

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["python", "/app/trainer.py"]
CMD ["--config", "/app/configs/baseline.yaml"]
