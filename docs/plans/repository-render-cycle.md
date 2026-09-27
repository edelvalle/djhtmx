# Repository-local render-cycle caches

Checked against `67686801434572797caeb04aefecb4866f66792e`.

## Context

Components reach a render through three transports: a page render that uses `{% htmx ... %}`, the HTMX endpoints under `/_htmx/`, and an SSE wakeup.  Since `CommandProcessor` was split out of `Repository`, the command loop is no longer the repository's, but every transport still renders through `Repository.render_html`, and nested renders still happen whenever a component's template calls `{% htmx ... %}` for a child.

The `Repository` is therefore still the right owner of render-cycle state.  It is built once per lifecycle and shared by every render in it: [`Repository.from_request`](https://github.com/edelvalle/djhtmx/blob/67686801434572797caeb04aefecb4866f66792e/src/djhtmx/repo.py#L68) for pages and endpoints, [`_drain_sse_session`](https://github.com/edelvalle/djhtmx/blob/67686801434572797caeb04aefecb4866f66792e/src/djhtmx/sse.py#L529) for one SSE wakeup batch, and `Repository.from_websocket` for the websocket stub.

One thing about the SSE path is new and shapes the later phases: the drain runs on a worker thread of the render executor.  `submit_sse_render` copies the caller's `contextvars.Context` and `ctx.run(...)`s the drain inside it, so the repository is built *inside* the copied context, not before it.

## Problems to solve

### SSE subscriptions are computed more than once

During one component render the framework computes `get_sse_subscriptions(component)` twice.  The first read is no longer in `render_html` itself: `render_html` calls `register_component(self.session.id, component)` and [`register_component`](https://github.com/edelvalle/djhtmx/blob/67686801434572797caeb04aefecb4866f66792e/src/djhtmx/sse.py#L161) computes the set itself.  The second is [`{% hx-tag %}`](https://github.com/edelvalle/djhtmx/blob/67686801434572797caeb04aefecb4866f66792e/src/djhtmx/templatetags/htmx.py#L132), which only needs to know whether the set is empty.

The duplicate now costs more than it did when this plan was written:

- If `sse_subscriptions` is time-sensitive, for example by calling `now()`, the two reads disagree: the consumer record is registered for one set of topics and the root tag advertises a consumer for another.
- `get_sse_subscriptions` is not a property read.  It resolves the event types `_handle_sse_events` accepts, substituting typevars for inherited handlers, and filters the declared subscriptions against them, per call.
- A `sse_subscriptions` that consults a model field pays that field twice.  `TodoItem.sse_subscriptions` opens with `if self.item:`, and on a lazy annotation `_LazyModelProxy.__bool__` resolves the row, so the second read is a second query.
- Both warnings inside that path -- the `is_sse_enabled` mismatch and "subscribes to X but `_handle_sse_events` does not accept it" -- are logged twice per render.

We want framework code to compute SSE subscriptions once per component within the current repository cycle and reuse that value consistently.

### Model annotations can rehydrate duplicate ORM instances

When a component field is annotated with a Django model, for example `item: TodoItem`, rehydrating two components with the same primary key performs repeated database fetches and returns distinct Python model instances.  This is often just an unnecessary query, and it also prevents active-record-like identity behaviour within the current render/request/wakeup cycle.

The components that duplicate the fetch are typically strangers to each other, in every transport.  An `Emit` reaches every subscriber in the session, and a query-string update wakes every component subscribed to that parameter, so one cycle renders components that hold the same row and have no way to know it.  If they were always a single nested tree, a parent could hand the instance down and no cache would be needed; because a render set is assembled by signals instead, the repository is the only thing placed to share the row between them.

The hydration now happens in a `BeforeValidator` rather than ad-hoc conversion code, which gives the cache a single place to live per shape: [`_ModelBeforeValidator._get_instance`](https://github.com/edelvalle/djhtmx/blob/67686801434572797caeb04aefecb4866f66792e/src/djhtmx/introspection.py#L224) for eager fields and [`_LazyModelProxy.__ensure_instance`](https://github.com/edelvalle/djhtmx/blob/67686801434572797caeb04aefecb4866f66792e/src/djhtmx/introspection.py#L178) for lazy ones.  Both build the same queryset (`select_related`/`prefetch_related` from the `ModelConfig`) and both end in `filter(pk=...).first()`.

We want repeated hydration of the same model primary key within the same repository cycle to return the same model instance when possible.

## Design direction

The `Repository` should own render-cycle caches.  These caches should not live as independent module-level globals.  If we introduce a contextvar-current repository later, the contextvar should locate the current `Repository`; the cache storage should still be on the repository instance.

`Repository.__init__` is unchanged since this plan was written, so the target shape still holds:

```python
class Repository:
    def __init__(self, user, session, params):
        self.user = user
        self.session = session
        self.session_signed_id = signer.sign(session.id)
        self.session_hash = compact_hash(session.id)
        self.params = params
        self._sse_subscriptions_cache = {}
        self._model_instance_cache = {}
```

The repository then exposes explicit cache APIs:

```python
repo.get_sse_subscriptions(component)
repo.get_model_instance(model, pk, model_config, allow_none=False)
```

## SSE subscriptions cache

The SSE subscriptions cache should be repository-local.  The first framework call for a component computes `get_sse_subscriptions(component)`, stores the result, and later framework calls in the same repository cycle reuse the stored value.

The key is component object identity, `id(component)`, because the immediate problem is duplicate reads on the same component instance during the same render path.  This avoids accidentally freezing subscriptions for a component id after the component has been rebuilt as a new Python object later in the same repository cycle -- which `CommandProcessor` does routinely, since a command stream can rebuild a component between renders.

The entry must keep the component alive, `{id(component): (component, subscriptions)}`, not the subscriptions alone: nothing else in the repository holds a reference to a component that was only rendered, and CPython reuses the id of a collected object, so a bare `id()` key can hand one component another's subscriptions.  A `WeakKeyDictionary` is not an option -- components are pydantic models, which are weak-referenceable but unhashable.  The strong reference costs one live component per render for the length of the cycle, which the session's own state already matches.

`Repository.render_html()` should use this API before registering the component:

```python
subscriptions = self.get_sse_subscriptions(component)
register_component(self.session.id, component, subscriptions=subscriptions)
```

`{% hx-tag %}` should also use the repository API:

```python
subscriptions = repo.get_sse_subscriptions(component)
if subscriptions:
    attrs["data-djhtmx-sse-consumer"] = consumer_id(repo.session.id, component.id)
```

`register_component()` can either require subscriptions as an argument or accept them optionally for backwards compatibility; [`render_html`](https://github.com/edelvalle/djhtmx/blob/67686801434572797caeb04aefecb4866f66792e/src/djhtmx/repo.py#L292) is its only caller in the framework, so the signature change is cheap either way.  The important invariant is that `Repository.render_html()` and `{% hx-tag %}` share the same computed value.  Note that `register_component` derives *two* things from the set -- the index keys and the `metadata_json` that decides register-versus-delete -- inside a `WATCH`/`MULTI` retry loop, so passing the set in also keeps those consistent with the tag.

## ORM identity/cache

The ORM cache should also be repository-local.  The initial identity-map key should be `(model_class, primary_key)` so repeated hydration of the same row returns the same Python instance within the current repository cycle.

The primary key has to be normalised before it becomes part of the key.  A pk arrives as whatever JSON carried -- a `str` for a `UUIDField`, an `int` or a `str` for an `AutoField` -- and `_LazyModelProxy` already coerces it with `model._meta.pk.to_python(value)` while `_get_instance` leaves it to the queryset.  Without the same coercion in the cache, `"1"` and `1` are two keys for one row and the map silently never hits.

The model annotation validator can use the current repository when one is available:

```python
repo = Repository.current(default=None)
if repo is not None:
    return repo.get_model_instance(self.model, value, self.model_config, self.allow_none)
return self._get_instance_uncached(value)
```

This is the tail of `_get_instance`, after its two short-circuits: a value that is already a model instance, and a `_LazyModelProxy` handed down from a parent component, are returned as they are and never reach the cache.  `_LazyModelProxy.__ensure_instance` should go through the same API, so lazy access participates in the identity map too -- and so a lazy field and an eager field pointing at the same row do not fetch it twice.

`_ModelBeforeValidator.from_modelclass` is `@cache`d per `(model, model_config, allow_none)`, so the validator objects are shared across repositories and must stay stateless: the map belongs to the repository the validator looks up at call time, never to the validator.

### Relations on a shared instance

A strict identity map where the first fetch wins surprises the second component: it asked for `prefetch_related` and got an instance without it.  Each cached entry therefore records the relations already loaded on its instance, as a map from relation path to the queryset of its `Prefetch`, `None` for the default one.  A string relation, a `select_related` path and a `Prefetch` without a queryset all record `None`; every intermediate path a relation traverses is recorded with `None` too, because Django loads it with the default queryset.

On a hit, the relations the annotation asks for are compared with the ones recorded:

- a path not loaded yet is missing, and `prefetch_related_objects([instance], ...)` loads it onto the shared instance -- Django skips the paths already there, and loads a forward relation as `select_related` would have;
- a path loaded with the same queryset -- both `None`, or the very same queryset object -- is already there;
- a path loaded with another queryset is a conflict, and that hydration gets a fetch of its own, not cached.

Equivalent querysets built as different objects count as a conflict: comparing querysets for equivalence is not reliable, and a database read is the safe answer.  A `Prefetch` shared by several annotations is declared once, as a module constant, so they share the instance.

One SSE-specific caveat to document with it: the render executor closes database connections around renders, so an instance cached during one wakeup and touched during the next would lazy-load its deferred fields on a different connection.  Keeping the map strictly per repository -- one per wakeup, never reused across them -- is what makes that a non-issue.

## Opting into the cache

The identity map is off unless asked for.  `ModelConfig` grows `cache: bool | None = None`, where `None` defers to `settings.DEFAULT_MODEL_CACHE`, itself `getattr(settings, "DJHTMX_DEFAULT_MODEL_CACHE", False)`.  An explicit `True` or `False` on the annotation wins over the setting.

The default is off for backwards compatibility: an application that never opts in keeps today's behaviour, one fetch per hydration and a distinct instance per component, and nothing it does to a component's model field can leak into another component through a shared object.  A project that wants the map everywhere flips `DJHTMX_DEFAULT_MODEL_CACHE` and overrides the exceptions per annotation.

`ModelConfig` is hashed -- it keys the `@cache` on `_ModelBeforeValidator.from_modelclass` -- so the new field must stay hashable, which `bool | None` is.

When the flag resolves to `False`, hydration takes the uncached path it takes today and the repository stores nothing for that model, so `InvalidateModelCache` below has nothing to do for it.

## InvalidateModelCache

A handler that writes to the database, or that knows better than the map, invalidates or primes it by yielding a command:

```python
@dataclass(slots=True)
class InvalidateModelCache:
    target: models.Model | tuple[type[models.Model], Any | Sequence[Any] | None]
```

| `target` | Effect on the repository's map |
| --- | --- |
| a model instance | stores it under `(type(instance), instance.pk)`, replacing whatever was there, or priming an entry that does not exist yet |
| `(model_class, None)` | drops every entry of that model |
| `(model_class, pk)` | drops that one entry |
| `(model_class, [pk, ...])` | drops those entries |

It joins the `Command` union so handlers can yield it, and gets a `case` in `CommandProcessor._run_command` that acts on `self.repo` and yields nothing -- it never reaches the browser, so it is not a `ProcessedCommand`.  The pks it carries are normalised the same way the cache key is, or a `str` pk from a handler's arguments misses the `UUID` the map is keyed by.

Priming is the more interesting half: a handler that has just fetched or created a row can hand it to the map before the children that need it are built, and their hydration costs nothing.

### What invalidation reaches

Invalidation happens in the middle of a cycle, and it does not rewrite the past.  A component that already holds an instance keeps that object; dropping the entry only changes what *later* hydrations get.  This is a property of the map, not a defect to work around: the alternative -- reaching into every component that holds the row -- is what makes shared mutable state unpredictable.

Lazy annotations are the exception, and deliberately so.  For `ModelConfig(lazy=...)` the map stores the `_LazyModelProxy` rather than the instance, so every component with that annotation holds the *same* proxy.  Invalidating it clears the row the proxy resolved, and the next access through any of those components fetches again.  One yield therefore refreshes every lazy holder of the row, which is what a handler that just wrote to it usually wants.

## Repository as contextvar-local

A future change may make the current `Repository` available through a context variable.  That would make the repository available to code paths that cannot receive it as an explicit argument, such as the pydantic validators used by model annotations.

This should be a lookup mechanism only:

```python
with Repository.activate(repo):
    ...
```

The caches themselves should remain on the repository instance.  This keeps the lifecycle clear and avoids independent ambient caches with unclear invalidation semantics.

Activation must be scoped with a token reset, and placed at the boundaries that own a lifecycle, not at `from_request` -- worker threads are reused, and a contextvar set on the way in and never reset hands a stale repository to the next request on that thread.  The boundaries are the `/_htmx/` endpoint and the middleware for page renders, and `_drain_sse_session` for SSE.  Activating inside the drain is also what makes the executor work: `submit_sse_render` copies the context before the repository exists, and the copy is what the drain runs in.

`CommandProcessor` already carries a `ContextVar` of exactly this shape for its command recorder, down to the reset in a `finally`; it is the local precedent to copy.

Validators that run outside any activation -- a component built in a test, or in application code with no repository -- must keep working through the uncached path, which is what the `default=None` lookup buys.

## Expected invariants

Within one repository lifecycle, framework reads of `sse_subscriptions` for the same component instance are stable.  This covers page renders, HTMX endpoint calls, SSE wakeups, and nested `{% htmx ... %}` renders.

Within one repository lifecycle, repeated hydration of the same Django model primary key can reuse one Python model instance.  This reduces duplicate queries and provides active-record-like identity semantics for components participating in the same render/request/wakeup cycle.

Repository-local caches are discarded when the repository lifecycle ends.  No explicit invalidation is needed across separate HTTP requests, endpoint calls, or SSE wakeup batches.

## Implementation phases

1. Add repository-local cache fields and a `Repository.get_sse_subscriptions(component)` method keyed by `id(component)`, holding the component in the entry.
2. Give `sse.register_component` a `subscriptions` argument, pass the cached set from `Repository.render_html()`, and read the same set in `{% hx-tag %}` so one computed value is reused during rendering.
3. Add tests with a component whose `sse_subscriptions` property increments a counter, proving one framework evaluation for a component render; drive them through `Htmx` and `Htmx.drain_sse_events` so the SSE wakeup path is covered alongside the page render.
4. Introduce current-repository activation with a context variable, scoped at the endpoint, the middleware, and `_drain_sse_session`.
5. Add `ModelConfig.cache` and `settings.DEFAULT_MODEL_CACHE`, defaulting to off.
6. Add `Repository.get_model_instance(...)` as the one entry point of both `_ModelBeforeValidator._get_instance` and `_LazyModelProxy.__ensure_instance`: it decides whether the cache applies and fetches otherwise.  The map caches instances, nested by model, and records the relations loaded on each so that a later annotation's missing relations are loaded onto the shared instance.
7. Add tests proving duplicate model primary-key hydration reuses the cached instance and avoids duplicate database fetches within one repository lifecycle, and that an annotation left at the default keeps fetching per hydration.
8. Add `InvalidateModelCache` to the `Command` union and to `CommandProcessor._run_command`, covering the instance, the whole model, and the one-or-many pk forms.
9. Add tests for priming with an instance, for dropping entries mid-cycle leaving already-hydrated components untouched, and for a lazy proxy shared by two components refreshing for both after one invalidation.
10. Count model cache hits and misses as metrics through `tracing.metric_incr`, which publishes to Sentry and to Logfire: a hit, a miss, an enrichment that loaded missing relations, and a conflict that fell back to a fetch of its own.
