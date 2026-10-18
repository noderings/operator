# Basic make targets for building and deploying the operator

IMAGE_REGISTRY ?= harbor.noderings.com
IMAGE_REPOSITORY ?= nrings/proxmox-operator
IMG ?= $(IMAGE_REGISTRY)/$(IMAGE_REPOSITORY)
TAG ?= dev
NAMESPACE ?= proxmox-operator
PLATFORMS ?= linux/amd64,linux/arm64
POETRY ?= $(shell command -v poetry 2>/dev/null || echo $(HOME)/.local/bin/poetry)

.PHONY: help
help:
	@echo "Targets:"
	@echo "  docker-buildx-setup  Set up Docker buildx builder"
	@echo "  docker-build         Build the operator image (multi-arch)"
	@echo "  docker-build-local   Build and load a single-arch image for local use"
	@echo "  docker-push          Build and push multi-arch operator image"
	@echo "  helm-install         Install/upgrade the Helm chart"
	@echo "  helm-push-crds       Package and push CRD charts to Harbor OCI"
	@echo "  helm-push-operators  Package and push operator charts to Harbor OCI"
	@echo "  helm-lint            Lint the Proxmox Helm chart (strict)"
	@echo "  helm-lint-solusvm    Lint the SolusVM Helm chart (strict)"
	@echo "  kubescape-scan       Scan rendered manifests with Kubescape NSA framework"
	@echo "  chart-security       Run helm-lint and kubescape-scan"
	@echo "  poetry-install       Install deps into .venv via Poetry"
	@echo "  poetry-export        Export requirements.txt from Poetry"
	@echo "  test                 Run pytest"
	@echo "  run                  Run Proxmox operator via Poetry env"
	@echo "  run-solusvm          Run SolusVM operator via Poetry env"
	@echo "  pre-commit-install   Install pre-commit hooks"
	@echo "  pre-commit-run       Run pre-commit on all files"

.PHONY: docker-buildx-setup
docker-buildx-setup:
	@docker buildx version >/dev/null 2>&1 || (echo "Error: docker buildx not available" && exit 1)
	@if ! docker buildx inspect multiarch-builder >/dev/null 2>&1; then \
		docker buildx create --name multiarch-builder --driver docker-container --use || true; \
		docker buildx inspect --bootstrap; \
	fi

.PHONY: docker-build
docker-build: docker-buildx-setup
	# Optionally sync lock file and export requirements from Poetry before building (ignored if Poetry absent)
	poetry lock --no-update || true
	poetry export -f requirements.txt --without-hashes -o requirements.txt || true
	@echo "Building multi-arch image for platforms: $(PLATFORMS)"
	@echo "Note: Multi-arch images cannot be loaded locally. Use 'docker-build-local' for a single-arch load."
	docker buildx build \
		--platform $(PLATFORMS) \
		--build-arg VERSION=$(TAG) \
		--build-arg BUILD_DATE=$$(date -u +%Y-%m-%dT%H:%M:%SZ) \
		--build-arg GIT_COMMIT=$$(git rev-parse --short HEAD 2>/dev/null || echo unknown) \
		-t $(IMG):$(TAG) \
		.

.PHONY: docker-build-local
docker-build-local: docker-buildx-setup
	poetry lock --no-update || true
	poetry export -f requirements.txt --without-hashes -o requirements.txt || true
	@echo "Building single-arch image for local use (platform: $(shell docker version --format '{{.Server.Os}}/{{.Server.Arch}}' 2>/dev/null || echo linux/amd64))"
	docker buildx build \
		--platform $(shell docker version --format '{{.Server.Os}}/{{.Server.Arch}}' 2>/dev/null || echo linux/amd64) \
		--build-arg VERSION=$(TAG) \
		--build-arg BUILD_DATE=$$(date -u +%Y-%m-%dT%H:%M:%SZ) \
		--build-arg GIT_COMMIT=$$(git rev-parse --short HEAD 2>/dev/null || echo unknown) \
		-t $(IMG):$(TAG) \
		--load \
		.

.PHONY: docker-push
docker-push: docker-buildx-setup
	# Optionally sync lock file and export requirements from Poetry before building (ignored if Poetry absent)
	poetry lock --no-update || true
	poetry export -f requirements.txt --without-hashes -o requirements.txt || true
	@echo "Building and pushing multi-arch image for platforms: $(PLATFORMS)"
	docker buildx build \
		--platform $(PLATFORMS) \
		--build-arg VERSION=$(TAG) \
		--build-arg BUILD_DATE=$$(date -u +%Y-%m-%dT%H:%M:%SZ) \
		--build-arg GIT_COMMIT=$$(git rev-parse --short HEAD 2>/dev/null || echo unknown) \
		-t $(IMG):$(TAG) \
		--push \
		.

.PHONY: helm-install
helm-install:
	kubectl create ns $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -
	helm upgrade --install proxmox-operator charts/proxmox-operator \
	  --namespace $(NAMESPACE) \
	  --set image.registry=$(IMAGE_REGISTRY) \
	  --set image.repository=$(IMAGE_REPOSITORY) \
	  --set image.tag=$(TAG)

.PHONY: helm-push-crds
helm-push-crds:
	bash scripts/publish-crd-charts.sh

.PHONY: helm-push-operators
helm-push-operators:
	bash scripts/publish-operator-charts.sh

.PHONY: helm-lint
helm-lint:
	helm lint --strict charts/proxmox-operator-crds
	helm dependency build charts/proxmox-operator
	helm lint --strict charts/proxmox-operator

.PHONY: helm-lint-solusvm
helm-lint-solusvm:
	helm lint --strict charts/solusvm-operator-crds
	helm dependency build charts/solusvm-operator
	helm lint --strict charts/solusvm-operator

.PHONY: kubescape-scan
kubescape-scan: helm-lint
	helm template proxmox-operator charts/proxmox-operator --namespace $(NAMESPACE) > /tmp/proxmox-operator-rendered.yaml
	@command -v kubescape >/dev/null 2>&1 || { echo "kubescape not found in PATH"; exit 1; }
	kubescape scan framework nsa --compliance-threshold 100 --exceptions .kubescape/exceptions.json /tmp/proxmox-operator-rendered.yaml

.PHONY: chart-security
chart-security: helm-lint kubescape-scan

.PHONY: poetry-install
poetry-install:
	$(POETRY) install

.PHONY: poetry-export
poetry-export:
	$(POETRY) lock --no-update
	$(POETRY) export -f requirements.txt --without-hashes -o requirements.txt

.PHONY: test
test:
	$(POETRY) run pytest

.PHONY: run
run:
	@test -x "$(POETRY)" || { echo "poetry not found. Install: pipx install poetry && export PATH=\"\$$HOME/.local/bin:\$$PATH\""; exit 127; }
	PYTHONPATH=src $(POETRY) run kopf run -A -m proxmox_operator.handlers --dev

.PHONY: run-solusvm
run-solusvm:
	@test -x "$(POETRY)" || { echo "poetry not found. Install: pipx install poetry && export PATH=\"\$$HOME/.local/bin:\$$PATH\""; exit 127; }
	PYTHONPATH=src $(POETRY) run kopf run -A -m solusvm_operator.handlers --dev

.PHONY: pre-commit-install
pre-commit-install:
	$(POETRY) run pre-commit install

.PHONY: pre-commit-run
pre-commit-run:
	$(POETRY) run pre-commit run --all-files
