# Publishing djhtmx to PyPI

This document describes how to publish the djhtmx package to PyPI.

## Prerequisites

1. Ensure you have PyPI and TestPyPI accounts
2. Configure PyPI credentials using UV's built-in authentication:
   ```bash
   # Set PyPI token
   export UV_PUBLISH_TOKEN="your-pypi-token"
   
   # Or configure interactively
   uv publish --help  # Shows authentication options
   ```

## Publishing Process

### 1. Update Version

Update the version in `src/djhtmx/__init__.py`:

```python
__version__ = "1.0.0"  # Update to your target version
```

### 2. Build the Package

```bash
make build
```

This will:
- Clean the `dist/` directory
- Build both source distribution (.tar.gz) and wheel (.whl)

### 3. Test on TestPyPI (Recommended)

```bash
make publish-test
```

This publishes to TestPyPI for testing before the official release.

### 4. Publish to PyPI

```bash
make publish
```

This publishes the package to the official PyPI repository.

## Manual Commands

If you prefer to run commands manually:

```bash
# Build
uv build

# Upload to TestPyPI
uv publish --index-url https://test.pypi.org/simple/

# Upload to PyPI
uv publish
```

## Authentication

UV supports several authentication methods:

1. **Environment Variable** (recommended):
   ```bash
   export UV_PUBLISH_TOKEN="your-pypi-token"
   ```

2. **Interactive prompt**: UV will prompt for credentials if not configured

3. **Keyring integration**: UV can use system keyring for credential storage

## Notes

- The current setup uses hatchling as the build backend
- Version is automatically extracted from `src/djhtmx/__init__.py`
- License, README, and other metadata are configured in `pyproject.toml`
- Static files (templates, JS) are included via MANIFEST.in
- No need for twine - UV handles publishing directly