"""
Long-lived Kafka consumer for Phase 2.

Subscribes to `paperless.uploads` and dispatches each event to processor.process_event().

Failure policy (Apr 20 milestone):
  - Processing error → log, commit the offset anyway, move on.
  - Broker connection error → crash, let Docker restart us.
  - Poison messages (bad JSON) → log and skip.

A dead-letter topic can be added later without changing the event-handling code.

Environment variables:
  KAFKA_BROKER        default: redpanda:9092
  KAFKA_TOPIC         default: paperless.uploads
  KAFKA_GROUP_ID      default: htr-preprocessing
  FASTAPI_URL         default: http://fastapi_server:8000
  HTR_ENDPOINT        default: /predict/htr (paperless-ml's ml-gateway uses /htr)
  PAPERLESS_URL       default: http://paperless-webserver-1:8000
  PAPERLESS_TOKEN     (required at runtime for the slicer to fetch documents)
  MINIO_ENDPOINT      default: minio:9000
  MINIO_ACCESS_KEY    default: admin
  MINIO_SECRET_KEY    default: paperless_minio
  MINIO_BUCKET        default: paperless-images
  ML_DB_HOST          default: postgres
  ML_DB_NAME          default: paperless
  ML_DB_USER          default: user
  ML_DB_PASSWORD      default: paperless_postgres
"""

import json
import logging
import os
import signal
import sys
import time

from kafka import KafkaConsumer
from kafka.errors import NoBrokersAvailable

from slicer import RegionSlicer
import processor


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("htr_consumer")


def _build_consumer() -> KafkaConsumer:
    broker = os.environ.get("KAFKA_BROKER", "redpanda:9092")
    topic  = os.environ.get("KAFKA_TOPIC", "paperless.uploads")
    group  = os.environ.get("KAFKA_GROUP_ID", "htr-preprocessing")

    # Retry connect forever — if redpanda isn't ready yet, wait for it.
    while True:
        try:
            c = KafkaConsumer(
                topic,
                bootstrap_servers=broker,
                group_id=group,
                auto_offset_reset="earliest",  # process any events we missed
                enable_auto_commit=False,
                value_deserializer=lambda v: json.loads(v.decode("utf-8")),
            )
            log.info("Connected to Kafka at %s, topic=%s, group=%s", broker, topic, group)
            return c
        except NoBrokersAvailable:
            log.warning("Kafka not ready at %s, retrying in 5s", broker)
            time.sleep(5)


def _build_slicer() -> RegionSlicer:
    return RegionSlicer(
        paperless_url   = os.environ.get("PAPERLESS_URL", "http://paperless-webserver-1:8000"),
        paperless_token = os.environ.get("PAPERLESS_TOKEN", ""),
        minio_endpoint  = os.environ.get("MINIO_ENDPOINT", "minio:9000"),
        minio_access_key= os.environ.get("MINIO_ACCESS_KEY", "admin"),
        minio_secret_key= os.environ.get("MINIO_SECRET_KEY", "paperless_minio"),
        minio_bucket    = os.environ.get("MINIO_BUCKET", "paperless-images"),
    )


def main() -> None:
    # Graceful shutdown on SIGTERM
    stop = False
    def _handle_signal(signum, _frame):
        nonlocal stop
        log.info("Received signal %d, stopping after current event", signum)
        stop = True
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)

    if not os.environ.get("PAPERLESS_TOKEN"):
        log.error("PAPERLESS_TOKEN env var is required (needed for slicer to fetch documents).")
        sys.exit(1)

    slicer = _build_slicer()
    consumer = _build_consumer()

    log.info("HTR preprocessing consumer ready. Waiting for events...")

    for msg in consumer:
        if stop:
            break
        event = msg.value
        log.info(
            "recv offset=%d partition=%d paperless_doc_id=%s",
            msg.offset, msg.partition, event.get("paperless_doc_id"),
        )
        try:
            processor.process_event(event, slicer)
        except Exception as exc:
            # Fail loud, don't crash
            log.exception("Failed to process event offset=%d: %s", msg.offset, exc)
        finally:
            # Commit the offset regardless — poison messages don't block the topic.
            consumer.commit()

    consumer.close()
    log.info("Consumer exited cleanly.")


if __name__ == "__main__":
    main()
