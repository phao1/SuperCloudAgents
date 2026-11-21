# Docker needs the docker group; `sg` makes this work in shells that predate the group change.
DOCKER = $(shell timeout 15 docker info >/dev/null 2>&1 && echo docker || echo "sg docker -c")
PY := .venv/bin/python
define dk
$(if $(filter docker,$(DOCKER)),$(1),sg docker -c '$(1)')
endef

.PHONY: venv up up-all down image migrate api worker test e2e e2e-live e2e-codex
venv:
	python3 -m venv .venv && .venv/bin/pip install -q -U pip && .venv/bin/pip install -q -e '.[dev]'
up:
	$(call dk,docker compose -f deploy/docker-compose.yml up -d --wait)
up-all:
	$(call dk,docker compose -f deploy/docker-compose.yml --profile gateway --profile s3 --profile oidc --profile temporal-pg up -d --wait)
down:
	$(call dk,docker compose -f deploy/docker-compose.yml --profile gateway --profile s3 --profile oidc --profile temporal-pg down)
image:
	$(call dk,docker build -q -t sca-sandbox:latest deploy/sandbox)
	$(call dk,docker build -q -t sca-egress:tls-v1 deploy/egress)
migrate:
	.venv/bin/alembic upgrade head
api:
	.venv/bin/uvicorn sca.api.app:create_app --factory --no-proxy-headers --host 0.0.0.0 --port 8000
worker:
	$(call dk,$(PY) -m sca.orchestration.worker)
test:
	$(call dk,$(PY) -m pytest -q -m "not e2e and not codex and not gateway")
e2e:
	$(call dk,$(PY) -m pytest -q -m "e2e and not codex and not live and not gateway")
e2e-live:
	$(call dk,$(PY) -m pytest -q -m "live")
e2e-codex:
	$(call dk,$(PY) -m pytest -q -m codex -s)

.PHONY: portal-setup portal-up portal-down portal-status
portal-setup:
	bash scripts/portal_demo.sh setup
portal-up:
	bash scripts/portal_demo.sh up
portal-down:
	bash scripts/portal_demo.sh down
portal-status:
	bash scripts/portal_demo.sh status
