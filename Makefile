CARGO_HOME ?= $(HOME)/.cargo
PATH := $(HOME)/.local/bin:$(CARGO_HOME)/bin:$(PATH)

PYTHON_VERSION ?= 3.13

SHELL := /bin/bash
PROJECT_NAME := djhtmx

UV ?= uv
UV_RUN ?= $(UV) run
UV_PYTHON_PREFERENCE ?= only-managed
RUN ?= $(UV_RUN)

REQUIRED_UV_VERSION ?= 0.7.3
bootstrap:
	@INSTALLED_UV_VERSION=$$(uv --version 2>/dev/null | awk '{print $$2}' || echo "0.0.0"); \
    DETECTED_UV_VERSION=$$(printf '%s\n' "$(REQUIRED_UV_VERSION)" "$$INSTALLED_UV_VERSION" | sort -V | head -n1); \
	if [ "$$DETECTED_UV_VERSION" != "$(REQUIRED_UV_VERSION)" ]; then \
		uv self update $(REQUIRED_UV_VERSION) || curl -LsSf https://astral.sh/uv/$(REQUIRED_UV_VERSION)/install.sh | sh; \
	fi
	@$(UV) python pin $(PYTHON_VERSION)
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


SERVER_CMD ?= granian --reload --reload-paths . --port 8000 --access-log --interface asginl fision.asgi:application

run: install
	@$(RUN) python src/tests/manage.py migrate
	@cd src/tests; $(RUN) $(SERVER_CMD)
.PHONY: run

test:
	@cd src/tests; $(RUN) coverage run --rcfile=../../pyproject.toml -m manage test
.PHONY: test

coverage-html: test
	@cd src/tests; $(RUN) coverage html --rcfile=../../pyproject.toml

coverage: test
	@cd src/tests; $(RUN) coverage report --rcfile=../../pyproject.toml
.PHONY: coverage

coverage-xml: test
	@cd src/tests; $(RUN) coverage xml --rcfile=../../pyproject.toml
.PHONY: coverage-xml


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

# PyPI publishing targets
build:
	@rm -rf dist/
	@$(UV) build
.PHONY: build

publish-test: build
	@echo "Publishing to TestPyPI..."
	@$(UV) publish --index-url https://test.pypi.org/simple/
.PHONY: publish-test

publish: build
	@echo "Publishing to PyPI..."
	@$(UV) publish
.PHONY: publish

check-dist: build
	@echo "Distribution files built successfully:"
	@ls -la dist/
	@echo "Note: Skipping twine check due to version compatibility issue with new metadata fields"
.PHONY: check-dist
