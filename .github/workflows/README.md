# GitHub Actions Workflows

This directory contains GitHub Actions workflows for automated code quality checks and testing.

## Workflows

### `ci.yml` - Continuous Integration
**Triggers**: Push to main/master, Pull Requests

**Jobs**:
- **Lint & Format Check**: Verifies code formatting and linting rules using `make lint`
- **Type Check**: Runs type checking with `make pyright` (non-blocking, continues on error)
- **Tests & Coverage**: Runs the test suite and generates coverage reports using `make coverage`

**Requirements**: Tests will only run if linting passes.

### `code-quality.yml` - Comprehensive Code Quality Check
**Triggers**: Manual dispatch, Weekly on Sundays

**Features**:
- Complete code quality assessment
- Detailed test coverage reporting
- HTML coverage report generation and artifact upload
- All checks with detailed output grouping

## Makefile Targets Used

The workflows leverage the following Makefile targets:

- `make install` - Install dependencies using uv
- `make lint` - Check formatting and linting (ruff)
- `make pyright` - Run type checking (basedpyright)
- `make test` - Run Django tests
- `make coverage` - Run tests with coverage reporting
- `make coverage-html` - Generate HTML coverage report

## Setup Requirements

1. **Codecov Integration** (optional): Add `CODECOV_TOKEN` to repository secrets for coverage reporting
2. **Python 3.12**: Workflows use Python 3.12 as specified in the Makefile
3. **uv Package Manager**: Uses uv 0.7.3 for fast dependency management

## Coverage Reports

- Text coverage reports are displayed in workflow logs
- HTML coverage reports are uploaded as artifacts (30-day retention)
- Coverage data can be sent to Codecov if token is configured

## Running Locally

You can run the same checks locally using:

```bash
make install    # Install dependencies
make lint       # Check formatting/linting
make pyright    # Type checking
make coverage   # Tests with coverage
```