CARGO_HOME ?= $(HOME)/.cargo
PATH := $(HOME)/.local/bin:$(CARGO_HOME)/bin:$(PATH)

PYTHON_VERSION ?= 3.12

SHELL := /bin/bash
PROJECT_NAME := djhtmx

UV ?= uv
UV_RUN ?= uv run
UV_PYTHON_PREFERENCE ?= only-managed
RUN ?= $(UV_RUN)

REQUIRED_UV_VERSION ?= 0.7.3
bootstrap:
	@INSTALLED_UV_VERSION=$$(uv --version 2>/dev/null | awk '{print $$2}' || echo "0.0.0"); \
    DETECTED_UV_VERSION=$$(printf '%s\n' "$(REQUIRED_UV_VERSION)" "$$INSTALLED_UV_VERSION" | sort -V | head -n1); \
	if [ "$$DETECTED_UV_VERSION" != "$(REQUIRED_UV_VERSION)" ]; then \
		uv self update $(REQUIRED_UV_VERSION) || curl -LsSf https://astral.sh/uv/$(REQUIRED_UV_VERSION)/install.sh | sh; \
	fi
	@echo $(PYTHON_VERSION) > .python-version
.PHONY: bootstrap

install: bootstrap
	@$(UV) sync --python-preference=$(UV_PYTHON_PREFERENCE) --frozen $(sync_extra_args) \
      || $(UV) sync --python-preference=$(UV_PYTHON_PREFERENCE) $(sync_extra_args)
.PHONY: install

sync_extra_args ?=
sync: bootstrap
	@$(UV) sync --python-preference=$(UV_PYTHON_PREFERENCE) --frozen $(sync_extra_args)
.PHONY: sync


lock: bootstrap
ifdef update_all
	@$(UV) sync -U $(sync_extra_args)
else
	@$(UV) sync $(sync_extra_args)
endif
.PHONY: lock

upgrade update: bootstrap
	@$(MAKE) lock update_all=1
.PHONY: update upgrade

format-python:
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

test:
	@cd src/tests; $(RUN) coverage run --rcfile=../../pyproject.toml -m manage test
.PHONY: test

coverage-html: test
	@cd src/tests; $(RUN) coverage html --rcfile=../../pyproject.toml


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
