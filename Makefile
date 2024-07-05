CARGO_HOME ?= $(HOME)/.cargo
PATH := $(HOME)/.rye/shims:$(CARGO_HOME)/bin:$(PATH)

RYE_EXEC ?= rye run
PYTHON_VERSION ?= 3.12

SHELL := /bin/bash
PROJECT_NAME := djhtmx

USE_UV ?= true
REQUIRED_UV_VERSION ?= 0.2.2
REQUIRED_RYE_VERSION ?= 0.34.0
bootstrap:
	@INSTALLED_UV_VERSION=$$(uv --version 2>/dev/null | awk '{print $$2}' || echo "0.0.0"); \
    UV_VERSION=$$(printf '%s\n' "$(REQUIRED_UV_VERSION)" "$$INSTALLED_UV_VERSION" | sort -V | head -n1); \
	if [ "$$UV_VERSION" != "$(REQUIRED_UV_VERSION)" ]; then \
		curl -LsSf https://astral.sh/uv/install.sh | sh; \
	fi
	@INSTALLED_RYE_VERSION=$$(rye --version 2>/dev/null | head -n1 | awk '{print $$2}' || echo "0.0.0"); \
	RYE_VERSION=$$(printf '%s\n' "$(REQUIRED_RYE_VERSION)" "$$INSTALLED_RYE_VERSION" | sort -V | head -n1); \
	if [ "$$RYE_VERSION" != "$(REQUIRED_RYE_VERSION)" ]; then \
		rye self update || curl -sSf https://rye-up.com/get | bash; \
	fi
	@rye config --set-bool behavior.use-uv=$(USE_UV)
	@rye pin --relaxed $(PYTHON_VERSION)

install: bootstrap
	@rye sync -f
.PHONY: install

sync: bootstrap
	@rye sync --no-lock
.PHONY: sync

lock: bootstrap
ifdef update_all
	@rye sync --update-all
else
	@rye sync
endif
.PHONY: lock

update: bootstrap
	@$(MAKE) lock update_all=1
.PHONY: update


format-python:
	@$(RYE_EXEC) isort src/
	@$(RYE_EXEC) ruff check --fix src/
	@$(RYE_EXEC) ruff format src/

format: format-python format-rescript
.PHONY: format format-python format-rescript


lint:
	@$(RYE_EXEC) ruff src/$(PROJECT_NAME)
	@$(RYE_EXEC) ruff format --check src/$(PROJECT_NAME)
	@$(RYE_EXEC) isort --check src/$(PROJECT_NAME)
.PHONY: lint


PYRIGHT_FILES ?= src/$(PROJECT_NAME)
pyright:
	@$(RYE_EXEC) basedpyright $(PYRIGHT_FILES)
.PHONY: pyright


run:
	@$(RYE_EXEC) python src/tests/manage.py migrate
	@$(RYE_EXEC) python src/tests/manage.py runserver
.PHONY: run


makemigrations:
	@$(RYE_EXEC) python src/tests/manage.py makemigrations
.PHONY: makemigrations


py:
	@$(RYE_EXEC) ipython
.PHONY: py

SHELL_CMD ?= shell_plus
shell:
	@$(RYE_EXEC) python src/tests/manage.py $(SHELL_CMD) || $(RYE_EXEC) python src/tests/manage.py shell
.PHONY: shell
