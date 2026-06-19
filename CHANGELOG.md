# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - Unreleased

### Added

- **A non-optional `user` annotation is now enforced as a login requirement**: a component that declares `user: Annotated[User, Field(exclude=True)]` (instead of the optional annotation inherited from `HtmxComponent`) refuses to be built without a logged-in user.

  Until now the annotation documented an intention that nothing checked: Django model fields are validated with a `PlainValidator` that returns `None` unchanged, so a component annotated with a required user still ran its handlers with `self.user` set to `None` whenever the session had died (an expired session on an open page, a logout in another tab, a POST without cookies to the `csrf_exempt` endpoints), and failed deep in whatever it wrote -- typically a NOT NULL violation on a `created_by` column, losing the user's edit with no feedback on screen.

  The rule covers every way a request can arrive without someone able to act: no user, a primary key that matches no row, an unsaved instance, and an account with `is_active=False`.  A component that admits `None` (`user: User | None`) gets `None` for all of them rather than a user it must not act as.  `djhtmx.component.is_usable_user` exposes the predicate.

  **This changes the behavior of existing components.**  Any component that already annotates a non-optional `user` becomes login-required without further declaration, so one mounted on a page anonymous visitors can reach now redirects them to the login page instead of rendering with `user=None`.  Keep the field optional (`user: User | None`, or simply don't redeclare it) for components that read no user, or that still make sense to a viewer whose session died.  `djhtmx.component.requires_logged_user(component_class)` reports which of the two a component is, for use in your own tests.

- **Model-typed fields now enforce their annotation**: a field annotated with a Django model validates through a `BeforeValidator` instead of a `PlainValidator`, so pydantic's own type check runs after the primary key is resolved.  A plain validator *replaces* the core schema, which meant the annotation had no runtime meaning: a non-optional `item: Item` happily held the `None` the validator returned unchanged, and the component failed later, far from the cause.  Passing `None` to a non-optional model field now raises a `ValidationError` (`is_instance_of`, located at the field); annotate the field as `Item | None` where `None` is a legitimate value.  Lazy fields are unaffected in every respect -- proxy identity, query counts, deleted-row behavior, and state round-trips are unchanged, and `ModelConfig` still applies.

  For the `user` field this replaces nothing: `Repository.build` reports a rejected user as `LoginRequired` whichever layer rejected it, so both redirect paths behave exactly as before.  `CommandProcessor.process` no longer inspects `ValidationError` shapes -- that single decision now lives in `Repository.build`, which also means an application raising the `is_instance_of`-at-`user` error from its own validator keeps getting the redirect.

- **Inline type information (PEP 561)**: djhtmx now ships a `py.typed` marker, so type checkers consume the package's own annotations instead of inferring types from source.  This also makes them honor the package's re-export rules: names merely re-imported into a module (e.g. `Iterable` in `djhtmx.sse`) and not listed in `__all__` are no longer offered as importable symbols, so editor auto-import stops suggesting `from djhtmx.sse import Iterable` and similar indirect imports.  Import public names from their documented modules.

- **Python 3.14 support**: djhtmx is now tested on a Python 3.13 + 3.14 matrix.  On 3.14 the dependency floors rise to the first releases shipping 3.14 wheels (`pydantic>=2.13`, `orjson>=3.11`, `lxml>=6`); 3.13 installs are unaffected.

- `aemit_sse_event`: async counterpart of `emit_sse_event` for publishing SSE events from `async def` handlers without blocking the event loop on the synchronous Redis client.

### Changed

- **Minimum Django is now 5.2**: djhtmx requires `django>=5.2` (was `>=4.1`).  Django releases before 5.2 are end-of-life upstream and were neither tested nor supported; 5.2 is the LTS line djhtmx is developed against and the first to support Python 3.14.

- **The HTTP and SSE endpoints are `async` views, but the dispatch runs as one synchronous job on a bounded worker pool.**  Each request (and each SSE drain, and each WebSocket message) submits the whole dispatch — component build, event handlers, query patching, template render, session flush — to the `DJHTMX_SYNC_WORKERS` pool, where it runs on a single thread that owns one Django DB connection.  Nothing in the request path touches the ORM on the event loop.  As a result **the process-wide Postgres connection count is bounded by `DJHTMX_SYNC_WORKERS`, independent of request or SSE-stream concurrency**, and an idle SSE stream holds *no* DB connection (it borrows one from the pool only while a drain is actually rendering).

- **Components may define `async def` event handlers** (including async generators that `yield` commands).  Sync and async handlers can be mixed freely, even across a single event cascade: handlers communicate through the command/event bus, never by calling each other directly.  The synchronous dispatcher runs sync handlers directly and wraps async handlers with `async_to_sync` on the pool thread, so any Django async ORM an async handler awaits runs on that same thread and shares its single bounded connection.

- **`ATOMIC_REQUESTS` is honoured per sync handler.**  Django's `make_view_atomic` *raises* `RuntimeError("You cannot use ATOMIC_REQUESTS with async views.")` for an async view whenever any database has `ATOMIC_REQUESTS` set, so the async HTTP/SSE views declare themselves non-atomic for **every** configured database.  Atomicity is instead applied per synchronous event handler: each sync handler is wrapped in `transaction.atomic` for every `ATOMIC_REQUESTS` database.  It is per-handler rather than per-request (a single dispatch can fan out to multiple handlers), and a handler that raises rolls back its own writes before the error is surfaced.

- **`DJHTMX_SSE_RENDER_WORKERS` is renamed to `DJHTMX_SYNC_WORKERS`** (old name still honoured as a deprecated alias).  The pool that formerly served only SSE renders now bounds *all* synchronous, ORM-touching work — full HTTP/SSE/WS dispatches, component construction, and rendering — so it is the single knob for the Django DB connection budget.  Size it together with your app's process count so that `DJHTMX_SYNC_WORKERS × processes` stays under Postgres `max_connections` (and any pgbouncer pool).

- **Model-typed component fields keep their pre-2.0 loading semantics.**  The `models.Model` field validator resolves a bare pk to an instance (an existing pk → instance, a missing pk → `None` for optional fields, or an error for required ones), and `ModelConfig(lazy=True)` still defers the query to first access via a proxy.  What changed is *where* it runs: because the whole dispatch runs on the sync-work pool thread, the resolution query is on a pooled connection, never the event loop.  (Constructing a component directly from a pk works as before — no pre-resolution step required.)

- Model-typed `Query` fields carry the pk in the URL via a pk adapter, with the instance resolved during build by the field validator like any other Model field.

### Fixed

- **`ModelConfig`'s `select_related`/`prefetch_related` were ignored on lazy fields**: the config never reached the proxy that makes the query, so the optimization was accepted, stored on the annotation and then silently dropped -- a lazy field with `select_related` paid a second query for the related object, exactly what the argument was there to avoid.  Lazy fields now build their query with the configured related fields, so the JOIN happens in the same query that loads the row and a prefetch is populated before the collection is read.

- **`ModelConfig(select_related=["x"])` crashed at import time**: the config is a cache key (`_ModelBeforeValidator.from_modelclass` is cached on it), so the list form -- the one the README documents -- raised `TypeError: unhashable type: 'list'` while the annotation was being built, which happens at class-definition time and therefore brought down the whole module on import.  `select_related` and `prefetch_related` now accept any `Sequence` and store it as a tuple.  A bare string is refused with a `TypeError` naming the argument: it satisfies `Sequence[str]`, so no type checker objects to `select_related="owner"`, and iterating it would silently ask for one related field per character.  The predicate behind that check, `is_field_name_sequence`, is a `TypeGuard` and is importable from `djhtmx.introspection`.

- **A lazy model field was truthy even when its row was gone**: `_LazyModelProxy` had no `__bool__`, so `if component.item:` answered "yes" for a deleted or never-existing row -- the one question the check is asked.  It now resolves the row (caching it, like any other access): an optional field is `False` when the row is missing, and a required field raises the same `ValueError` that any other access raises, rather than quietly passing the check and failing on the next line.

- **`datetime`-typed `Query` fields rejected**: a component may now declare a `Query` field annotated with `datetime`.  Previously this raised `TypeError: Invalid type annotation ... for a query string` during component build.

- **Postgres connections no longer scale with concurrency.**  Earlier async iterations of this work resolved the user and Model fields with Django async ORM on the event loop; because `django.db.connections` is per-async-task, each in-flight request/stream acquired its own connection (held for the whole stream lifetime for SSE), exhausting `max_connections` under load.  Routing the entire dispatch through the bounded sync-work pool confines every ORM touch to a pooled, thread-affine connection.

- The SSE drain no longer risks a pool re-entrancy deadlock: each drain is a single synchronous job that never re-submits to the pool, so concurrent drains beyond the worker count simply queue.

## [1.3.13] - 2026-06-17

### Added

- Experimental SSE channel with a single per-page `SSEEventRouter` and a bounded render thread pool. New settings: `DJHTMX_SSE_RENDER_WORKERS` (default `8`), `DJHTMX_SSE_RENDER_QUEUE_MAX`, `DJHTMX_SSE_RENDER_HEALTHCHECK_EVERY`, `DJHTMX_SSE_RENDER_ROTATE_EVERY`.

- SSE render-pool metrics published through Sentry and Logfire under the `djhtmx.sse.render.*` namespace: `queue_depth` (distribution), `queue_wait_ms` (distribution), `duration_ms` (distribution), `drops` (counter), `rotations` (counter), `healthcheck_closes` (counter), and `broken_connection_closes` (counter).  New helpers `metric_incr`,  `metric_distribution` in `djhtmx.tracing` mirror the existing `tracing_span` dual-backend pattern.

### Changed

- Command classes (`Destroy`, `Redirect`, `Open`, `Focus`, `ScrollIntoView`, `DispatchDOMEvent`, `Render`, `BuildAndRender`, `Emit`, `Execute`, `SkipRender`, `PushURL`, `ReplaceURL`, `SendHtml`, and the `Command` union) now live in `djhtmx.commands`.  Update imports to `from djhtmx.commands import …`.  The `Command` union is also narrower: `Signal` and `SendHtml` are framework internals and no longer in it.

- The optional `sentry` extra now requires `sentry-sdk>=2.62` (was `>=2.19`).  Sentry removed the experimental DDM metrics API (`sentry_sdk.metrics.incr`) in 2.41 and replaced it with the Trace Metrics API, so the SSE render-pool counters now publish through `sentry_sdk.metrics.count` / `distribution`.  Calling the removed `incr` raised `AttributeError` and broke the SSE render path on any consumer that had upgraded `sentry-sdk`.

### Fixed

- **Malformed query values for Model-typed `Query` fields**: `QueryPatcher` now also catches Django's `ValidationError` (not only `ValueError`) when validating a value from the query string, falling back to the field default instead of failing the request.  Resolving a Model `Query` field from its PK runs `Model.objects.filter(pk=value)`, and a value the PK field cannot parse (e.g. a non-UUID string for a `UUIDField`) raises `django.core.exceptions.ValidationError`, which is *not* a subclass of `ValueError`.  A stray `?param=garbage` in the URL therefore raised during component build and returned an HTTP 500.  Both `get_update_for_state` and `get_updates_for_params` are fixed.

- **Inherited `Query` fields of Django Model type**: `QueryPatcher.for_component` now reconstructs the full annotation from `field.annotation` + `field.metadata` instead of reading `component.__annotations__[field_name]`.  The previous lookup only inspected the class's own annotations (no MRO walk) and silently fell through to the bare annotation for inherited fields, dropping the `PlainSerializer` that `annotate_model` attaches for Model and QuerySet types.  Components that inherited a field like `Annotated[MyModel | None, Query(...), Field(default=None)]` from an abstract base would raise `PydanticSerializationError: Unable to serialize unknown type` the moment the URL had to be updated.

- Python 3.14 `DeprecationWarning` by using `asgiref.sync.iscoroutinefunction` for async middleware detection.  `asyncio.iscoroutinefunction` is deprecated in 3.14 (removal in 3.16); asgiref's variant is what Django itself uses to detect async middleware/views, so it also honours callables marked with `markcoroutinefunction`.

## [1.3.12] - 2026-04-24

### Added
- **Sentry tag forwarding**: Non-sensitive HTMX request headers (e.g. `HX-Current-URL`) are now attached as tags to the active Sentry scope and to djhtmx tracing spans for HTTP endpoints. `HX-Prompt` is intentionally excluded because it may contain private user input.

## [1.3.11] - 2026-03-11

### Fixed
- **Redirect header precedence**: When a `Redirect` and a `ReplaceURL` or `PushURL` command are both produced in the same HTTP request cycle, `HX-Replace-Url` and `HX-Push-Url` headers are now stripped from the response. Previously HTMX would process the URL manipulation header and silently ignore `HX-Redirect`, preventing the redirect from happening.

## [1.3.10] - 2026-03-06

### Fixed
- **Lazy Model Proxy PK type coercion**: Fixed `_LazyModelProxy.pk` returning a raw string instead of the correct PK type (e.g., UUID) after JSON deserialization. This caused Pydantic serialization warnings (`Expected uuid`) when re-serializing components with `ModelConfig(lazy=True)` fields. The proxy now coerces the PK value through Django's field `to_python()` on construction.

## [1.3.9] - 2026-03-06

### Fixed
- **Generic Event Handlers**: Fixed event subscription for components that inherit `_handle_event` from a generic base class without overriding it. Previously, generic type parameters (e.g., `MyEvent[NodeT]`) in inherited handlers were not resolved to concrete types, causing subclasses to silently miss events. The introspection now resolves TypeVar bindings from the class hierarchy and correctly extracts event types from generic aliases.

## [1.3.8] - 2026-02-20

### Added
- **ScrollIntoView `if_not_visible` option**: Added `if_not_visible: bool = False` parameter to `ScrollIntoView` command. When set to `True`, the scroll only occurs if the element is not fully visible in the viewport. This prevents unnecessary scrolling when the element is already in view.

### Removed
- Removed the Codecov badge and CI upload step, along with related workflow documentation.

## [1.3.7] - 2026-02-13

### Added
- **Testing Helper**: Added `Htmx.url` property to compose the current path and query string without a trailing `?`.

### Changed
- **Python Version**: Raised the minimum supported Python version to 3.13 and aligned tooling/CI with Python 3.13.
- **Dev Server**: Switched the development server dependency and `make run` command to Granian.

### Fixed
- **Pydantic Validator Warning**: Changed from `BeforeValidator` to `PlainValidator` for Django model field annotations to eliminate Pydantic 2.8 warning about validators returning non-self values. This is purely a technical fix with no functional changes.
- **Testing Runtime Warning**: Replaced lxml truth-testing in `djhtmx.testing.Htmx` with explicit `is not None` checks to avoid future `FutureWarning` behavior.
- **Query Annotations on Python 3.13**: Improved handling of `Annotated`/`Optional` model query annotations and serialization of optional model values to avoid validation and serialization errors.

## [1.3.6] - 2026-02-03

### Fixed
- **Class Template Tag**: Boolean expression evaluation now uses the yesno filter behavior, where string values "True" and "False" (case-insensitive) are properly converted to boolean values before evaluation

## [1.3.5] - 2026-02-02

### Fixed
- **htmx_class Template Tag**: Fixed handling of `None` values in conditional class names. Previously, `None` values were converted to the string "None" in the output. Now they are properly filtered out before joining class names.

## [1.3.4] - 2026-01-19

**Note**: This release supersedes versions 1.3.1, 1.3.2, and 1.3.3, which contained incomplete implementations of the `Model | None` handling feature. Users on 1.3.1-1.3.3 should upgrade to 1.3.4 immediately.

### Added
- **Yield Logging**: Added debug logging for commands yielded from component methods during event handling. Logs format: `< YIELD: ComponentName.method_name -> Command(...)`. Helps developers track command flow and debug issues when components emit multiple commands.
- Comprehensive test coverage for `Model | None` and lazy model handling with 12 new tests

### Fixed
- **Model | None Handling**: Fixed components with `Model | None` fields to gracefully return `None` when objects don't exist or have been deleted, instead of raising `DoesNotExist` exceptions
  - Changed database lookups from `manager.get()` to `manager.filter().first()` for graceful handling
  - For optional fields (`Model | None`), returns `None` when object doesn't exist
  - For required fields (`Model`), raises clear `ValueError` with descriptive message
  - Works correctly with both eager and lazy loading (`ModelConfig(lazy=True)`)
  - Lazy models create proxies that handle non-existent objects when attributes are accessed
- **Type Safety**: Added None check for `app_config.module` in `autodiscover_htmx_modules()` to prevent AttributeError

### Technical Details
- Added `allow_none` parameter to `_ModelBeforeValidator` and `_LazyModelProxy` classes
- Enhanced `annotate_model()` to detect `Model | None` unions and pass `allow_none=True`
- Updated lazy proxy `__ensure_instance()` to use `filter().first()` and handle missing objects gracefully
- QuerySet fields continue to work correctly by silently filtering out non-existent IDs

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
