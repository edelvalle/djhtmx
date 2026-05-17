# Repository-local render-cycle caches

## Context

Components can currently be rendered through three entry points: a normal HTTP template render that uses `{% htmx ... %}`, an HTMX callback endpoint, and an SSE wakeup.  All three paths eventually render components through a `Repository` instance, and nested component renders are also possible because a component template may call `{% htmx ... %}` for child components.

The `Repository` is therefore the right owner for local render-cycle state.  It is already local to the current HTTP request, HTMX callback, or SSE wakeup batch, and it is shared by nested component renders in that cycle.

## Problems to solve

### SSE subscriptions are computed more than once

During one component render, the framework currently calls `get_sse_subscriptions(component)` in two places: `register_component()` before template rendering, and `{% hx-tag %}` while rendering the component root tag.  If `sse_subscriptions` is implemented as a time-sensitive property, for example by calling `now()`, those two reads can produce different subscriptions for the same component render.

We want framework code to compute SSE subscriptions once for a component within the current repository cycle and reuse that value consistently.

### Model annotations can rehydrate duplicate ORM instances

When a component field is annotated with a Django model, for example `item: TodoItem`, rehydrating two components with the same primary key currently performs repeated database fetches and returns distinct Python model instances.  This is often just an unnecessary query, and it also prevents active-record-like identity behavior within the current render/request/wakeup cycle.

We want repeated hydration of the same model primary key within the same repository cycle to return the same model instance when possible.

## Design direction

The `Repository` should own render-cycle caches.  These caches should not live as independent module-level globals.  If we introduce a contextvar-current repository later, the contextvar should locate the current `Repository`; the cache storage should still be on the repository instance.

Target shape:

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

The likely key is component object identity, e.g. `id(component)`, because the immediate problem is duplicate reads on the same component instance during the same render path.  This avoids accidentally freezing subscriptions for a component id after the component has been rebuilt as a new Python object later in the same repository cycle.

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

`register_component()` can either require subscriptions as an argument or accept them optionally for backwards compatibility.  The important invariant is that `Repository.render_html()` and `{% hx-tag %}` share the same computed value.

## ORM identity/cache

The ORM cache should also be repository-local.  The initial identity-map key should be `(model_class, primary_key)` so repeated hydration of the same row returns the same Python instance within the current repository cycle.

The model annotation validator can use the current repository when one is available:

```python
repo = Repository.current(default=None)
if repo is not None:
    return repo.get_model_instance(self.model, value, self.model_config, self.allow_none)
return self._get_instance_uncached(value)
```

The same approach can be used by lazy model proxy materialization, so lazy access also participates in the repository identity map.

A later design decision is how to handle `ModelConfig(select_related=...)` and `ModelConfig(prefetch_related=...)`.  A strict identity map means `(model, pk)` wins and later requests return the existing instance, even if a richer fetch plan is requested.  A fetch-plan-aware cache can avoid exact duplicate queries but may return different Python instances for the same row.  The first implementation should prefer identity-map behavior and document the fetch-plan tradeoff.

## Repository as contextvar-local

A future change may make the current `Repository` available through a context variable.  That would make the repository available to code paths that cannot receive it as an explicit argument, such as Pydantic validators used by model annotations.

This should be a lookup mechanism only:

```python
with Repository.activate(repo):
    ...
```

The caches themselves should remain on the repository instance.  This keeps the lifecycle clear and avoids independent ambient caches with unclear invalidation semantics.

## Expected invariants

Within one repository lifecycle, framework reads of `sse_subscriptions` for the same component instance are stable.  This covers normal HTTP renders, HTMX callbacks, SSE wakeups, and nested `{% htmx ... %}` renders.

Within one repository lifecycle, repeated hydration of the same Django model primary key can reuse one Python model instance.  This reduces duplicate queries and provides active-record-like identity semantics for components participating in the same render/request/wakeup cycle.

Repository-local caches are discarded when the repository lifecycle ends.  No explicit invalidation is needed across separate HTTP requests, callback requests, or SSE wakeup batches.

## Implementation phases

1. Add repository-local cache fields and a `Repository.get_sse_subscriptions(component)` method.
2. Change `Repository.render_html()`, `{% hx-tag %}`, and `register_component()` so one computed SSE subscription set is reused during rendering.
3. Add tests with a component whose `sse_subscriptions` property increments a counter, proving one framework evaluation for a component render.
4. Introduce current-repository activation with a context variable.
5. Add `Repository.get_model_instance(...)` and wire model annotation hydration and lazy proxy materialization through it when a current repository is available.
6. Add tests proving duplicate model primary-key hydration reuses the cached instance and avoids duplicate database fetches within one repository lifecycle.
