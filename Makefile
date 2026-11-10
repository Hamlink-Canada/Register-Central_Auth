UV ?= uv
.PHONY: sync check test build
sync:
	$(UV) sync --locked
check:
	$(UV) run --locked ruff check .
	$(UV) run --locked ruff format --check .
test:
	$(UV) run --locked pytest
build:
	$(UV) build
