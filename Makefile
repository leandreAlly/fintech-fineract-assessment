# One entry point per task. `make help` lists targets.
SHELL := /bin/bash
FINERACT_TAG ?= 1.15.0
JDK21_BREW := /opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home
export JAVA_HOME ?= $(or $(wildcard $(JDK21_BREW)),$(shell /usr/libexec/java_home -v 21 2>/dev/null))
GRADLE := ./gradlew --no-daemon -Porg.gradle.java.installations.paths=$(JAVA_HOME)
FINERACT_DIR := fineract-customization/fineract
PY_SERVICES := mock-erp integration-service

.PHONY: help up down reset logs fineract-checkout fineract-patch fineract-test fineract-it setup demo-clients demo-loan demo-erp demo-failures demo-reconciliation demo-trace demo-a demo-a-sync fineract-image py-sync py-lint py-test test-live test

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

fineract-test: ## Run the JUnit unit tests for the GL sync customization
	cd $(FINERACT_DIR) && $(GRADLE) :fineract-accounting:test --tests '*GlAccountExternalReference*'

fineract-it: ## Run the JUnit integration test against the running Fineract (make up first)
	cd $(FINERACT_DIR) && $(GRADLE) :integration-tests:test -PcargoDisabled --tests '*GlAccountExternalReference*'

setup: ## Configure Fineract: RWF, Kigali office, API user, synced GL accounts, loan product
	scripts/fineract-setup.sh

demo-clients: ## Scripted demo of client identities (create, find, validate, idempotency)
	scripts/demo-clients.sh

demo-loan: ## Scripted loan lifecycle: application to Closed, Fineract state after every stage
	scripts/demo-loan-lifecycle.sh

demo-erp: ## Scripted demo of ERP journal entries and balances
	scripts/demo-erp-journal.sh

demo-failures: ## Part J: five failure scenarios (stops ERP/Fineract, kills the service)
	scripts/demo-failures.sh

demo-reconciliation: ## Part K: seed all four reconciliation outcomes, report, repair
	scripts/demo-reconciliation.sh

demo-trace: ## Observability: trace one repayment through logs and one API call
	scripts/demo-trace.sh

demo-a: ## Scripted demo of Part A behaviours 1-4 against the running stack
	scripts/demo-part-a.sh

demo-a-sync: ## Scripted demo of the Python GL sync (stops and restarts Fineract)
	scripts/demo-part-a-sync.sh

fineract-image: ## Build the custom Fineract image from source + patch
	docker compose build fineract

py-sync: ## Install Python dependencies for both services
	@for s in $(PY_SERVICES); do (cd $$s && uv sync); done

py-lint: ## Ruff + mypy for both services
	@for s in $(PY_SERVICES); do (cd $$s && uv run ruff check . && uv run ruff format --check . && uv run mypy app tests $$(ls -d tests_live 2>/dev/null)) || exit 1; done

py-test: ## pytest for both services
	@for s in $(PY_SERVICES); do (cd $$s && uv run pytest -q) || exit 1; done

test-live: ## Part I parallel tests against the running stack (duplicates, concurrency)
	cd integration-service && uv run pytest tests_live -s -v -p no:cacheprovider

test: py-lint py-test fineract-test ## Everything
