# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.2.0] - 2025-09-29

### Added
- **Enhanced HTMX Module Discovery**: HTMX components can now be organized in directory structures within Django apps. The autodiscovery system now recursively imports all Python modules under `htmx/` directories, in addition to the traditional single `htmx.py` files. This allows for better code organization in larger projects.
- **New Management Commands**:
  - `python manage.py htmx check-unused`: Check for unused HTMX components in your project
  - `python manage.py htmx check-unused-non-public`: Check for unused non-public HTMX components

### Changed
- **BREAKING**: Template name validation now raises `ImproperlyConfigured` exceptions instead of logging warnings when HTMX component template names don't match the component class name. This provides better error visibility and prevents potential runtime issues.

### Technical Details
- Modified `apps.py` to use the new autodiscovery function instead of Django's standard `autodiscover_modules("htmx")`
- Added `autodiscover_htmx_modules()` function in `utils.py` that recursively discovers and imports all Python modules in `htmx/` directories across Django apps
- Maintains full backward compatibility with existing single `htmx.py` files

### Migration Guide
- **Template Name Validation**: If you have components with mismatched template names, you'll now get `ImproperlyConfigured` exceptions instead of warnings. Update your component template names to match the class names.
- **Module Organization**: You can now organize your HTMX components in directory structures under `htmx/` in your Django apps. No changes required for existing single `htmx.py` files.

## [1.1.2] - 2025-08-27

### Fixed
- **Python 3.13 Compatibility**: Added support for `defaultdict[..., ...]` type annotations in Query introspection
- Fixed type checking for generic aliases in collection annotations

### Documentation
- **Redis Dependency**: Added clear documentation that Redis is required and must be installed separately
- **Framework Clarification**: Clarified that djhtmx is a framework, not a component library - no pre-built components are provided
- **Settings Documentation**: Added comprehensive documentation for all available Django settings
- **Installation Guide**: Added Redis installation instructions for different platforms

## [1.1.1] - 2025-08-23

- Remove `get_model_subscriptions` `Action` annotation as string literals, as this does not reflect model relationships

## [1.1.0] - 2025-08-15

### Changed
- **BREAKING**: Refactored `get_model_subscriptions` to use explicit action parameters instead of auto-subscribing to all actions by default
- Changed default behavior from implicit subscription to all actions to explicit opt-in only
- Updated command queue to subscribe to both instance and model-level signals for better coverage

### Added
- **Custom Context Support for Render Command**: The `Render` command now accepts an optional `context` parameter of type `dict[str, Any]`. When provided, this context will override the component's default context during template rendering, while preserving essential HTMX variables (`htmx_repo`, `hx_oob`, `this`). This enables more flexible template rendering scenarios where you need to pass custom data that differs from the component's state.
- Added type annotation `Action = Literal["created", "updated", "deleted"]` for better type safety
- Support for `None` in actions parameter to include bare prefix subscriptions

### Technical Details
- The `Render` dataclass now includes a `context: dict[str, Any] | None = None` field  
- The `Repository.render_html` method signature now includes an optional `context` parameter
- Template rendering logic now supports context override while maintaining backwards compatibility
- All custom context changes are fully backwards compatible - existing code continues to work without modification
- When `context=None` (default), behavior is identical to previous versions
- When `context` is provided, it takes precedence over component context but essential HTMX context variables are preserved
- Comprehensive test coverage added for all new functionality

### Migration Guide
- If you were relying on `get_model_subscriptions()` to automatically subscribe to all actions, you now need to explicitly pass the actions you want: `get_model_subscriptions(instance, actions=["created", "updated", "deleted"])`
