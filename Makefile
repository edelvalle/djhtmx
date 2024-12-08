CARGO_HOME ?= $(HOME)/.cargo
PATH := $(HOME)/.rye/shims:$(CARGO_HOME)/bin:$(PATH)

PYTHON_VERSION ?= 3.12

SHELL := /bin/bash
PROJECT_NAME := djhtmx

ifdef INSIDE_EMACS
	UV ?= NO_COLOR=1 UV_PYTHON=${PYTHON_VERSION} uv
	UV_RUN ?= $(UV) run
	RYE_RUN ?= NO_COLOR=1 rye run
else
	UV ?= UV_PYTHON=${PYTHON_VERSION} uv
	UV_RUN ?= $(UV) run
	RYE_RUN ?= rye run
endif

# In some cases we want to run things with rye to avoid bug
# https://github.com/astral-sh/uv/pull/6738
RUN ?= $(UV_RUN)

REQUIRED_UV_VERSION ?= 0.4.27
REQUIRED_RYE_VERSION ?= 0.41.0
bootstrap-uv:
	@INSTALLED_UV_VERSION=$$(uv --version 2>/dev/null | awk '{print $$2}' || echo "0.0.0"); \
    DETECTED_UV_VERSION=$$(printf '%s\n' "$(REQUIRED_UV_VERSION)" "$$INSTALLED_UV_VERSION" | sort -V | head -n1); \
	if [ "$$DETECTED_UV_VERSION" != "$(REQUIRED_UV_VERSION)" ]; then \
		uv self update | curl -LsSf https://astral.sh/uv/install.sh | sh; \
	fi

bootstrap: bootstrap-uv
	@INSTALLED_RYE_VERSION=$$(rye --version 2>/dev/null | head -n1 | awk '{print $$2}' || echo "0.0.0"); \
	DETECTED_RYE_VERSION=$$(printf '%s\n' "$(REQUIRED_RYE_VERSION)" "$$INSTALLED_RYE_VERSION" | sort -V | head -n1); \
	if [ "$$DETECTED_RYE_VERSION" != "$(REQUIRED_RYE_VERSION)" ]; then \
		rye self update || curl -sSf https://rye.astral.sh/get | RYE_INSTALL_OPTION="--yes" RYE_VERSION="$(REQUIRED_RYE_VERSION)" bash; \
	fi
.PHONY: bootstrap-uv bootstrap

install: bootstrap uv.lock
	@$(UV) sync --frozen

upgrade: bootstrap
	@$(UV) sync
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
