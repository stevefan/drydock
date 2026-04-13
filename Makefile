.DEFAULT_GOAL := help

.PHONY: help test lint install base-publish rebuild clean-registry

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

test: ## Run pytest
	.venv/bin/pytest -q

lint: ## Run pyflakes
	.venv/bin/python -m pyflakes src/ tests/

install: ## Create venv and install in dev mode
	python3 -m venv .venv
	.venv/bin/pip install -e '.[dev]'

base-publish: ## Build and push base image (requires VERSION=...)
	@test -n "$(VERSION)" || (echo "error: VERSION is required (e.g. make base-publish VERSION=0.2.0)" && exit 1)
	./base/build-and-push.sh $(VERSION)

rebuild: ## Destroy and recreate a workspace (requires NAME=...)
	@test -n "$(NAME)" || (echo "error: NAME is required (e.g. make rebuild NAME=myproject)" && exit 1)
	.venv/bin/ws destroy $(NAME) --force
	.venv/bin/ws create $(NAME)

clean-registry: ## Delete the local registry database
	rm -f ~/.drydock/registry.db
