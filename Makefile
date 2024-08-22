CARGO_HOME ?= $(HOME)/.cargo
PATH := $(HOME)/.rye/shims:$(CARGO_HOME)/bin:$(PATH)

PYTHON_VERSION ?= 3.12

SHELL := /bin/bash
PROJECT_NAME := djhtmx
RUN := uv run
REQUIRED_UV_VERSION ?= 0.3.0
bootstrap:
	@INSTALLED_UV_VERSION=$$(uv --version 2>/dev/null | awk '{print $$2}' || echo "0.0.0"); \
    UV_VERSION=$$(printf '%s\n' "$(REQUIRED_UV_VERSION)" "$$INSTALLED_UV_VERSION" | sort -V | head -n1); \
	if [ "$$UV_VERSION" != "$(REQUIRED_UV_VERSION)" ]; then \
		curl -LsSf https://astral.sh/uv/install.sh | sh; \
	fi

install: bootstrap uv.lock
	@uv sync --frozen

upgrade: bootstrap
	@uv sync
.PHONY: ugprade

format-python:
	@$(RUN) isort src/
	@$(RUN) ruff check --fix src/
	@$(RUN) ruff format src/

format: format-python format-rescript
.PHONY: format format-python format-rescript


lint:
	@$(RUN) ruff check src/$(PROJECT_NAME)
	@$(RUN) ruff format --check src/$(PROJECT_NAME)
.PHONY: lint


PYRIGHT_FILES ?= src/$(PROJECT_NAME)
pyright:
	@$(RUN) basedpyright $(PYRIGHT_FILES)
.PHONY: pyright


run: install
	@$(RUN) python src/tests/manage.py migrate
	@cd src/tests; $(RUN) uvicorn --reload --reload-include="*.html" --reload-dir=../ fision.asgi:application
.PHONY: run


makemigrations:
	@$(RUN) python src/tests/manage.py makemigrations
.PHONY: makemigrations


py:
	@$(RUN) ipython
.PHONY: py

SHELL_CMD ?= shell_plus
shell:
	@$(RUN) python src/tests/manage.py $(SHELL_CMD) || @$(RUN) src/tests/manage.py shell
.PHONY: shell
