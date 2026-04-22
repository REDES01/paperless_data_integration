FROM apache/airflow:2.10.4

# Pre-install DockerOperator provider at build time instead of at every
# container start via _PIP_ADDITIONAL_REQUIREMENTS. That env var approach
# is officially dev-only — the startup install was crash-looping the
# webserver + scheduler, probably from transient pypi issues or the
# container exiting before pip completed.

USER airflow
RUN pip install --no-cache-dir 'apache-airflow-providers-docker'
