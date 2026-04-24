FROM apache/airflow:2.10.4

# Pre-install providers + libraries the DAGs need:
#   - DockerOperator: apache-airflow-providers-docker
#   - search_feedback_rerank aggregate task: psycopg2-binary
#   - archive_corrections DAG: minio SDK for S3-compatible uploads
# Pinning lets Docker's layer cache skip this step on subsequent builds.

USER airflow
RUN pip install --no-cache-dir \
    'apache-airflow-providers-docker' \
    'psycopg2-binary>=2.9,<3' \
    'minio>=7.2,<8'
