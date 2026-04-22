# HTR trainer — fine-tunes TrOCR, logs to MLflow.
#
# Built from paperless_data_integration/:
#   docker build -f training/Dockerfile -t htr_trainer:latest .

FROM python:3.12-slim

WORKDIR /app

# CPU-only torch from pytorch's dedicated index
RUN pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu \
    torch==2.4.1

COPY training/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Pre-download the printed base model into the image — this is our new
# baseline since trocr-base-handwritten was already trained on IAM and gave
# us a ceiling-baseline (no room for fine-tuning to help).
RUN python -c "\
from transformers import TrOCRProcessor, VisionEncoderDecoderModel;\
TrOCRProcessor.from_pretrained('microsoft/trocr-base-printed');\
VisionEncoderDecoderModel.from_pretrained('microsoft/trocr-base-printed');\
print('TrOCR-base-printed cached')"

COPY training/ /app/

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["python", "/app/trainer.py"]
CMD ["--config", "/app/configs/baseline.yaml"]
