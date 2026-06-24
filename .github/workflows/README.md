# GitHub Actions Workflows

This directory contains GitHub Actions workflows for automated code quality checks and testing.

## Workflows

### `ci.yml` - Continuous Integration
**Triggers**: Push to main/master, Pull Requests

**Jobs**:
- **Lint & Format Check**: Verifies code formatting and linting rules using `make lint`
- **Type Check**: Runs type checking with `make typecheck` (zuban); advisory (non-blocking) until zuban is green on the async refactor
- **Tests & Coverage**: Runs the test suite (against a Redis service) on a Python **3.13 + 3.14** matrix and generates a coverage report using `make coverage-xml`

**Requirements**: Tests will only run if linting passes.

## Makefile Targets Used

The workflows leverage the following Makefile targets:

- `make install` - Install dependencies using uv
- `make lint` - Check formatting and linting (ruff)
- `make typecheck` - Run type checking (zuban)
- `make test` - Run Django tests
- `make coverage` - Run tests with coverage reporting
- `make coverage-html` - Generate HTML coverage report

## Setup Requirements

1. **Python 3.13 / 3.14**: lint and type-check run on 3.14; tests run on a 3.13 + 3.14 matrix (the matrix version is threaded into `make install` via `PYTHON_VERSION`)
2. **uv Package Manager**: Uses uv 0.10.10 (matching `REQUIRED_UV_VERSION` in the Makefile)

## Coverage Reports

- Text coverage reports are displayed in workflow logs
- HTML coverage reports are uploaded as artifacts (30-day retention)

## Running Locally

You can run the same checks locally using:

```bash
make install    # Install dependencies
make lint       # Check formatting/linting
make typecheck  # Type checking
make coverage   # Tests with coverage
```