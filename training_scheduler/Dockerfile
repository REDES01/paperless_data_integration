# Training scheduler — fires the training pipeline on a recurring schedule.
#
# Uses the standard docker:cli image so we can invoke `docker compose` on
# the host via the mounted socket. No Python, no additional deps.
#
# Build:
#   docker build -t training_scheduler:latest paperless_data_integration/training_scheduler/

FROM docker:27-cli-alpine3.20

# docker-compose-plugin isn't included in the stock docker CLI image on
# Alpine, but `docker compose` works if the Docker CLI speaks to a host
# daemon that has compose installed — which is our case (host has
# compose v2 plugin, we only forward socket). So no extra install here.

COPY scheduler.sh /scheduler.sh
RUN chmod +x /scheduler.sh

ENV INTERVAL_MIN=60 \
    CANDIDATE=finetune_combined \
    COMPOSE_PROJECT=training \
    COMPOSE_FILE=/compose.yml

ENTRYPOINT ["/scheduler.sh"]
