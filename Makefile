.PHONY: build test up down logs shell status cli data
export UID := $(shell id -u)
export GID := $(shell id -g)

# Create the bind-mount source as the host user first — otherwise the Docker
# daemon creates ./data as root and the non-root container can't write config.
data:
	mkdir -p data

# Build a single self-contained executable (dist/brokerscrub). scp it to any
# host with Python 3.11+, chmod +x, and run — no Docker, no venv, no install.
binary:
	mkdir -p dist
	python3 -m venv /tmp/bs-build
	/tmp/bs-build/bin/pip install -q shiv
	/tmp/bs-build/bin/shiv -c brokerscrub -o dist/brokerscrub --python "/usr/bin/env python3" .
	rm -rf /tmp/bs-build
	@echo "built dist/brokerscrub — scp to a server, chmod +x, run ./brokerscrub"

build:
	docker compose build

# full suite (units + GreenMail e2e loop) inside Docker.
# --build so a code change is always retested against fresh images.
test:
	docker compose --profile test up -d --force-recreate greenmail
	@docker compose --profile test run --rm --build e2e; rc=$$?; \
	docker compose --profile test rm -sf greenmail >/dev/null 2>&1; \
	exit $$rc

up: data
	docker compose up -d brokerscrub

down:
	docker compose down

logs:
	docker compose logs -f brokerscrub

shell:
	docker compose run --rm --entrypoint bash brokerscrub

# one-off CLI, e.g.: make cli ARGS="status"
cli: data
	docker compose run --rm brokerscrub $(ARGS)

status: data
	docker compose run --rm brokerscrub status
