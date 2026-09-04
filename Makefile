SHELL := /bin/sh
.DEFAULT_GOAL := up

COMPOSE ?= docker compose
PRODUCTION ?= 0
WAIT_TIMEOUT ?= 180

ifneq ($(filter 0 1,$(PRODUCTION)),$(PRODUCTION))
$(error PRODUCTION muss 0 oder 1 sein)
endif

COMPOSE_FILES := -f docker-compose.yml
ifeq ($(PRODUCTION),1)
COMPOSE_FILES += -f docker-compose.prod.yml
endif

COMPOSE_CMD = $(COMPOSE) $(COMPOSE_FILES)

.PHONY: up down restart update assets scan-images

assets:
	npm ci --ignore-scripts
	npm run build

scan-images:
	./scripts/scan-images.sh

up:
	$(COMPOSE_CMD) config --quiet
	$(COMPOSE_CMD) up --build --detach --wait --wait-timeout $(WAIT_TIMEOUT) --remove-orphans

down:
	$(COMPOSE_CMD) down --remove-orphans

restart:
	$(COMPOSE_CMD) config --quiet
	$(COMPOSE_CMD) up --build --detach --force-recreate --wait \
		--wait-timeout $(WAIT_TIMEOUT) --remove-orphans

update:
	$(COMPOSE_CMD) config --quiet
	$(COMPOSE_CMD) pull --ignore-buildable
	$(COMPOSE_CMD) build --pull
	@if [ -n "$$($(COMPOSE_CMD) ps --status running --quiet app)" ]; then \
		echo "Erstelle Sicherheitsbackup vor dem Update ..."; \
		$(COMPOSE_CMD) exec -T app python -m app.cli backups create; \
	else \
		echo "App läuft nicht; Sicherheitsbackup wird übersprungen."; \
	fi
	$(COMPOSE_CMD) up --detach --wait --wait-timeout $(WAIT_TIMEOUT) --remove-orphans
