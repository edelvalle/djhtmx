# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Custom Context Support for Render Command**: The `Render` command now accepts an optional `context` parameter of type `dict[str, Any]`. When provided, this context will override the component's default context during template rendering, while preserving essential HTMX variables (`htmx_repo`, `hx_oob`, `this`). This enables more flexible template rendering scenarios where you need to pass custom data that differs from the component's state.

### Changed
- The `Render` dataclass now includes a `context: dict[str, Any] | None = None` field
- The `Repository.render_html` method signature now includes an optional `context` parameter
- Template rendering logic now supports context override while maintaining backwards compatibility

### Technical Details
- All changes are fully backwards compatible - existing code continues to work without modification
- When `context=None` (default), behavior is identical to previous versions
- When `context` is provided, it takes precedence over component context but essential HTMX context variables are preserved
- Comprehensive test coverage added for the new functionality