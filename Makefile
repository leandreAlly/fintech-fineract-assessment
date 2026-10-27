# One entry point per task. `make help` lists targets.
SHELL := /bin/bash
FINERACT_TAG ?= 1.15.0
export JAVA_HOME ?= $(shell /usr/libexec/java_home -v 21 2>/dev/null)
FINERACT_DIR := fineract-customization/fineract
PY_SERVICES := mock-erp integration-service

.PHONY: help up down reset logs fineract-checkout fineract-patch fineract-test fineract-image py-sync py-lint py-test test

help: ## List targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

up: ## Build and start the whole stack
	docker compose up --build -d

down: ## Stop the stack (keeps data)
	docker compose down

reset: ## Stop the stack and delete all data (empty Fineract DB on next `up`)
	docker compose down -v

logs: ## Follow logs of all services
	docker compose logs -f

fineract-checkout: ## Clone the pinned Fineract tag and apply our patch
	FINERACT_TAG=$(FINERACT_TAG) scripts/fineract-checkout.sh

fineract-patch: ## Export the Fineract working-tree changes to patches/erp-gl-sync.patch
	FINERACT_TAG=$(FINERACT_TAG) scripts/fineract-export-patch.sh

fineract-test: ## Run the Java tests for the GL sync customization (JUnit)
	cd $(FINERACT_DIR) && ./gradlew --no-daemon :fineract-provider:test --tests '*GlAccountExternalReference*'

fineract-image: ## Build the custom Fineract image from source + patch
	docker compose build fineract

py-sync: ## Install Python dependencies for both services
	@for s in $(PY_SERVICES); do (cd $$s && uv sync); done

py-lint: ## Ruff + mypy for both services
	@for s in $(PY_SERVICES); do (cd $$s && uv run ruff check . && uv run ruff format --check . && uv run mypy app tests) || exit 1; done

py-test: ## pytest for both services
	@for s in $(PY_SERVICES); do (cd $$s && uv run pytest -q) || exit 1; done

test: py-lint py-test fineract-test ## Everything
