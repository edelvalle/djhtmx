PATH := $(HOME)/.local/bin:$(PATH)

PYTHON_VERSION ?= 3.13
SHELL := /bin/bash
PROJECT_NAME := djhtmx

# Load .envrc when inside Emacs, Claude Code, or pi
_LOAD_ENVRC :=
ifdef INSIDE_EMACS
    _LOAD_ENVRC := 1
endif
ifdef CLAUDECODE
    _LOAD_ENVRC := 1
endif
ifeq ($(LLM_AGENT_PUPPETEER),pi)
    _LOAD_ENVRC := 1
endif
ifdef _LOAD_ENVRC
    ifneq (,$(wildcard .envrc))
        export BASH_ENV := $(CURDIR)/.envrc
        # Source .envrc and import all exported variables into Make Write to temp file to preserve
        # spaces and special characters Filter out Make-reserved variables that shouldn't be
        # overridden
        _ := $(shell bash -c 'set -a; source $(CURDIR)/.envrc 2>/dev/null && env | grep -v "^SHELL=" | grep -v "^MAKEFLAGS=" | grep -v "^MFLAGS=" | grep -v "^MAKEFILE_LIST=" > $(CURDIR)/.envrc.make.tmp')
        include .envrc.make.tmp
    endif
endif

UV ?= uv
UV_RUN ?= $(UV) run
UV_PYTHON_PREFERENCE ?= only-managed
RUN ?= $(UV_RUN)

REQUIRED_UV_VERSION ?= 0.10.10
bootstrap:
	@INSTALLED_UV_VERSION=$$(uv --version 2>/dev/null | awk '{print $$2}' || echo "0.0.0"); \
    DETECTED_UV_VERSION=$$(printf '%s\n' "$(REQUIRED_UV_VERSION)" "$$INSTALLED_UV_VERSION" | sort -V | head -n1); \
	if [ "$$DETECTED_UV_VERSION" != "$(REQUIRED_UV_VERSION)" ]; then \
		uv self update $(REQUIRED_UV_VERSION) 2>/dev/null || { \
			success=false; \
			for i in 1 2 3 4 5; do \
				if curl -LsSf https://astral.sh/uv/$(REQUIRED_UV_VERSION)/install.sh | sh; then \
					success=true; \
					break; \
				else \
					echo "curl attempt $$i failed, retrying in 1 second..."; \
					sleep 1; \
				fi; \
			done; \
			if [ "$$success" != "true" ]; then \
				echo "All curl attempts failed"; \
				exit 1; \
			fi; \
		}; \
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


SERVER_CMD ?= granian --reload --reload-paths fision --reload-paths ../djhtmx --port 8000 --access-log --workers 1 --workers-kill-timeout 1s --interface asginl fision.asgi:application

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
