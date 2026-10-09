# Review of `feat/async-pipeline`

Range reviewed: `git diff f2e86e932c091b663c0947d9717f407d753f9240...66605878a93740c0969e728ab7454b577bebd59d` (`master...feat/async-pipeline`), 17 commits over 48 files, focused on `src/djhtmx/*`.

The findings are ranked by severity.

| # | Finding | Status |
|---|---------|--------|
| 1 | Async-generator handlers skip argument validation | Fixed |
| 2 | `sse_endpoint` resolves the user from the session only | Open |
| 3 | `aemit_sse_event` leaks a Redis connection per dispatch | Open |
| 4 | `_LazyModelProxy` no longer forwards underscore attributes | Open |

## 1. Async-generator event handlers skip argument validation (medium) — fixed

This is not a regression against `master`: there, `async def` handlers never worked, and since 1.4.0 they are refused at import because they were silently never awaited (see the 1.4.0 "Fixed" entry in `CHANGELOG.md`).  This branch is the one that brings async handlers back (the 2.0.0 "Changed" entry), so it is this branch's job to make them honour the same contract as sync handlers, argument conversion included.

`src/djhtmx/component.py:279` wraps every event handler that has parameters with `validate_call`, but excludes async generator functions:

```python
# `validate_call` does not support async generator functions.
# Leave them unwrapped.
and not isasyncgenfunction(attr)
```

The premise is false across the whole declared range (`pydantic>=2.10,<3`, `>=2.13` on Python 3.14): pydantic 2.10.0, 2.11.0, 2.12.0, 2.13.0 and 2.13.4 all wrap `async def g(x: int): yield x`, coerce `'3'` to `3`, and expose `raw_function`.  The wrapper itself is not an async generator function on any of them, which is fine: djhtmx detects a handler's shape on `raw_function`.  Since `filter_parameters` passes the raw form values, a handler such as `async def stream(self, count: int)` receives `count="3"`, and arithmetic or comparisons on it fail or misbehave.  Every other handler shape (sync, sync generator, coroutine) gets coerced arguments, so as the branch stands the contract of a handler's annotations depends on how it is written.

Fix: drop the `isasyncgenfunction` exclusion (and its comment) and let `validate_call` wrap async generators like the rest.

Resolution:

- Regression test, committed as `db1d9005fbf54d371a62dbbef755f30b5b6eaddd`: `AgentChat.send` declares `prompt: StrippedStr` instead of stripping by hand, and `TestAsyncGeneratorHandlerArguments` (`src/tests/fision/todo/tests.py`) checks that a padded prompt is stored stripped and a blank one records nothing.  The `Makefile` `test` target sets `AI_PROVIDER_MODEL=test` so pydantic-ai's `TestModel` mounts the chat UI in CI.
- Fix, committed as `5913723ef7e71f38356992be0002e1b56ef02967`: the exclusion is gone from `src/djhtmx/component.py`.  Handler-shape detection already looks through `validate_call`, so a wrapped async generator still dispatches as one.  The stale comment in `AgentChat.send` claiming `validate_call` breaks coroutine handlers with arguments is removed: pydantic keeps them coroutine functions and exposes `raw_function`.
- `make test`: 212 tests pass, both with the locked pydantic 2.13.4 and with pydantic 2.10.0 (the declared floor) on Python 3.13.

## 2. `sse_endpoint` resolves the user from the session only (medium)

`src/djhtmx/urls.py:86` resolves the SSE user with `django.contrib.auth.get_user(request)`, which reads the session directly and ignores the `request.user` installed by the middleware stack.  On `master` the endpoint used `getattr(request, "user", None)`.

Failure scenarios:

- A project that authenticates without sessions (`RemoteUserMiddleware`, token or header auth, a custom middleware) now sees `AnonymousUser` on the SSE stream; components that require a user fail or render as anonymous.
- A project without `SessionMiddleware` raises `AttributeError` on `request.session`, turning a working endpoint into a 500.

Fix: resolve `request.user` (forcing the lazy object on the sync-work pool thread, which is why `_resolve_user` exists) and keep the `getattr(..., None)` tolerance of `master`.

## 3. `aemit_sse_event` leaks a Redis connection per dispatch (low)

`aemit_sse_event` (`src/djhtmx/sse.py:389`) is meant for `async def` handlers.  Outside tests, those handlers run through `async_to_sync` on a pool thread, which creates a fresh event loop per dispatch.  `get_async_conn()` (`src/djhtmx/sse.py:447`) caches one `redis.asyncio` client per loop in a `WeakKeyDictionary` and never closes it.

Consequence: every async-handler dispatch that emits opens a new Redis connection that is never closed when its loop ends.  The socket lingers until garbage collection and produces "Unclosed connection" warnings; under load this leaks connections and file descriptors.

Fix options: close the client when the loop finishes (e.g. scope the connection to the dispatch instead of caching it per loop), or route async emission through a connection owned by a long-lived loop.

## 4. `_LazyModelProxy` no longer forwards underscore attributes (low)

`_LazyModelProxy.__getattr__` (`src/djhtmx/introspection.py:186`) now raises `AttributeError` for any name starting with `_`, to keep pydantic and `copy` probing from triggering a query.  On `master` it forwarded every attribute to the model instance.

Code that uses a lazy model field as an instance and touches private model attributes breaks where it used to work, e.g. `self.item._meta.verbose_name`, `model_to_dict(self.item)` (reads `instance._meta`), or `self.item._state.adding`.

Fix: narrow the guard to dunder names (`__*__`) and the proxy's own slots, and forward single-underscore names such as `_meta` and `_state` (`_meta` could even be served from the model class without a query).

## Considered and dropped

- Coroutine handlers with parameters work: `validate_call` keeps them async and exposes `raw_function`.  The stale comment claiming otherwise was removed with the fix of finding 1.
- Async-generator handlers hold a pool thread for their whole run.  The test app already documents this as a known limitation.
