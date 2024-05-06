CARGO_HOME ?= $(HOME)/.cargo
PATH := $(HOME)/.rye/shims:$(CARGO_HOME)/bin

RYE_EXEC ?= rye run
PYTHON_VERSION ?= 3.12

SHELL := /bin/bash
PROJECT_NAME := djhtmx

USE_UV ?= true
install:
	@curl -LsSf https://astral.sh/uv/install.sh | sh
	@rye self update || curl -sSf https://rye-up.com/get | bash
	@rye config --set-bool behavior.use-uv=$(USE_UV)
	@rye pin --relaxed $(PYTHON_VERSION)
	@rye sync -f
.PHONY: install

sync:
	@rye config --set-bool behavior.use-uv=$(USE_UV)
	@rye pin --relaxed $(PYTHON_VERSION)
	@rye sync --no-lock
.PHONY: sync


lock:
	@rye config --set-bool behavior.use-uv=$(USE_UV)
	@rye pin --relaxed $(PYTHON_VERSION)
	@rye sync
.PHONY: lock

format-python:
	@$(RYE_EXEC) isort src/$(PROJECT_NAME)
	@$(RYE_EXEC) ruff check --fix src/$(PROJECT_NAME)
	@$(RYE_EXEC) ruff format src/$(PROJECT_NAME)

format: format-python format-rescript
.PHONY: format format-python format-rescript


lint:
	@$(RYE_EXEC) ruff src/$(PROJECT_NAME)
	@$(RYE_EXEC) ruff format --check src/$(PROJECT_NAME)
	@$(RYE_EXEC) isort --check src/$(PROJECT_NAME)
.PHONY: lint


PYRIGHT_FILES ?= src/$(PROJECT_NAME)
pyright:
	@$(RYE_EXEC) pyright $(PYRIGHT_FILES)
.PHONY: pyright


run:
	@$(RYE_EXEC) python src/tests/manage.py migrate
	@$(RYE_EXEC) python src/tests/manage.py runserver
.PHONY: run


py:
	@$(RYE_EXEC) ipython
.PHONY: py

SHELL_CMD ?= shell_plus
shell:
	@$(RYE_EXEC) python src/tests/manage.py $(SHELL_CMD) || $(RYE_EXEC) python src/tests/manage.py shell
.PHONY: shell
