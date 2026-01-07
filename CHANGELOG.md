# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.3.0] - 2026-01-07

### Changed
- **HTMX Module Discovery**: Improved module discovery mechanism to use `find_spec` instead of try/except for checking module existence. This allows ImportErrors from within HTMX modules to propagate properly, preventing silent failures and making debugging easier. Previously, import errors from within the module itself were silently caught, masking real bugs.
- Removed warning messages about missing HTMX modules for cleaner logging

## [1.2.9] - 2026-01-07

### Added
- **ScrollIntoView Command**: New command that scrolls elements into view with configurable behavior (`auto`, `smooth`, `instant`) and block alignment (`start`, `center`, `end`, `nearest`). Includes WebSocket and HTTP trigger support with strict Python typing via Literal types.

### Changed
- **Import Error Handling**: Import errors during HTMX module autodiscovery are no longer suppressed. This change improves error visibility and helps catch import issues in user code earlier. Previously, import errors were silently caught, which could hide configuration or dependency problems.
- Refactored `django.js` to use modern JavaScript patterns (for...of loops, const declarations) for improved code quality and maintainability

## [1.2.8] - 2026-01-06

### Added
- Added idiomorph extension (idiomorph-ext.min.js) for HTMX 2.0.4

## [1.2.7] - 2026-01-05

### Changed
- The `htmx` template tag now accepts component types directly as the first parameter, automatically extracting the component name. You can now use `{% htmx MyComponent data=value %}` instead of `{% htmx 'MyComponent' data=value %}`

## [1.2.6] - 2025-12-24

### Changed
- The `oob` template tag now automatically converts its suffix parameter to a string, allowing non-string values (integers, floats, etc.) to be passed directly

## [1.2.5] - 2025-12-05

### Fixed
- Fixed Destroy of component to use proper hx-swap-oob="delete" syntax

## [1.2.4] - 2025-10-29

### Fixed
- Prioritize Destroy commands to prevent stale component access - fixes race condition where signals would awaken components whose model instances were already deleted

### Changed
- Code quality improvements (Ruff suggestions for ternary operators)

## [1.2.3] - 2025-10-09

### Fixed
- Fixed OOB tag to use component ID directly instead of context 'id' key for more consistent behavior

### Changed
- Refactored context merging logic in Repository.render() for better code clarity
- Removed CSRF meta tags

## [1.2.2] - 2025-10-06

### Fixed
- Fixed tests related to component template names
- Removed CSRF verification on endpoint

### Changed
- Removed redundant code-quality workflow

## [1.2.1] - 2025-10-01

### Added
- **Literal Type Support in Query Objects**: Query objects can now use `Literal` type annotations for parameters. The introspection system now recognizes `Literal` types with simple values (str, int, float, bool, etc.) as basic types, enabling more precise type constraints in HTMX component queries.

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
