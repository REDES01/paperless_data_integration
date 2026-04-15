# Convenience targets for the integration layer.
# Assumes paperless_data/ and paperless-ngx-fork/ are cloned as siblings of this repo.

SHELL := /bin/bash
REPO_ROOT   := $(abspath $(dir $(firstword $(MAKEFILE_LIST))))
WORKSPACE   := $(abspath $(REPO_ROOT)/..)

PAPERLESS_COMPOSE  := $(REPO_ROOT)/paperless/docker-compose.yml
PAPERLESS_OVERRIDE := $(REPO_ROOT)/overrides/paperless.override.yml
PAPERLESS_PROJECT  := paperless

DATA_COMPOSE       := $(WORKSPACE)/paperless_data/docker/docker-compose.yaml
DATA_OVERRIDE      := $(REPO_ROOT)/overrides/paperless_data.override.yml
DATA_PROJECT       := paperless_data

.PHONY: help network paperless-up paperless-data-up up verify down clean

help:
	@echo "Targets:"
	@echo "  network           Create the shared paperless_ml_net docker network"
	@echo "  paperless-data-up Bring up the data stack with the override applied"
	@echo "  paperless-up      Bring up Paperless with the override applied"
	@echo "  up                network + paperless-data-up + paperless-up"
	@echo "  verify            Check cross-stack DNS from inside paperless-webserver-1"
	@echo "  down              Stop both stacks"

network:
	@./scripts/create_network.sh

paperless-data-up:
	docker compose -p $(DATA_PROJECT) -f $(DATA_COMPOSE) -f $(DATA_OVERRIDE) up -d

paperless-up:
	docker compose -p $(PAPERLESS_PROJECT) -f $(PAPERLESS_COMPOSE) -f $(PAPERLESS_OVERRIDE) up -d

up: network paperless-data-up paperless-up

verify:
	@./scripts/verify.sh

down:
	-docker compose -p $(PAPERLESS_PROJECT) -f $(PAPERLESS_COMPOSE) -f $(PAPERLESS_OVERRIDE) down
	-docker compose -p $(DATA_PROJECT) -f $(DATA_COMPOSE) -f $(DATA_OVERRIDE) down

clean: down
	-docker network rm paperless_ml_net
