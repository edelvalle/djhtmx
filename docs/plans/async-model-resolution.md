# Plan: async Model resolution, pure validators, no lazy

Status: **superseded** (2.0).  The "pure validators / no lazy / breaking" plan
below was the async-pipeline-era design; it has been reverted.

What actually shipped in 2.0: because the whole dispatch runs synchronously on
the sync-work pool thread (see `docs/async-architecture.md`), the Model-field
**validator resolves a bare pk itself** (sync ORM, on the pooled connection) —
exactly as before 2.0 — and `ModelConfig(lazy=True)` is **still supported**
(deferred `_LazyModelProxy`).  So there is **no breaking change** for Model
fields: constructing a component directly from a pk works, with no
pre-resolution step.  The detailed plan below (pure validators, removed lazy,
`resolve_model_fields`, `afirst`) is kept only as a record of the path not taken.

## Goal

Make `models.Model`-typed component fields load **before** pydantic validation,
asynchronously, so that:

1. **Field validators do no DB access.** A validator only checks that it was
   handed an already-loaded instance (or `None`); given anything else (e.g. a
   bare pk) it **raises**. No querying, no lazy proxy.
2. **The async build fetches instances with Django async ORM** (`afirst`),
   awaited on the event loop, then constructs the component with the instances.
3. **`lazy` Model support is removed entirely** (`_LazyModelProxy`,
   `ModelConfig.lazy`). If a user wants laziness they manage it themselves.
   This is a **breaking change**.

Serialization is unchanged: a Model field still dumps to its pk.

## Locked decisions

- Async fetch uses **`afirst`** always (`await M.objects…filter(pk=pk).afirst()`).
  Per the connection-model choice, these run on Django's async-ORM
  (thread-sensitive) connection — *not* the `DJHTMX_SYNC_WORKERS` pool. The
  "one pool bounds all DB connections" invariant gains a documented exception:
  build-time model fetches use Django's shared async-ORM connection.
- The validator is **pure and strict**: instance → ok, `None` → ok (when
  `allow_none`), anything else → **raise**. We deliberately let it fail rather
  than fall back to a sync query.

## Current design (what we're replacing)

- `annotate_model(Item|None)` →
  `Annotated[_LazyModelProxy[Item] | Item | None, PlainValidator(_ModelBeforeValidator), PlainSerializer(pk)]`.
- `_ModelBeforeValidator.__call__` resolves a pk → instance **inside validation**
  (sync ORM), or returns a `_LazyModelProxy` for `lazy=True` (defers the query to
  attribute access).
- Component build (`_build_from_state`) constructs the model; the validator does
  the query. The async path currently offloads the whole build to the sync-work
  pool (`eager_models=True`, the interim fix in `feat/async-pipeline`).
- **Query fields**: `QueryPatcher.get_update_for_state` calls
  `adapter.validate_python(pk)` where `adapter` is built from the field's full
  annotation — so a Model query field resolves pk → instance **through the same
  validator** (sync ORM), inside patching.

## Target design

### 1. Pure validator (`introspection.py`)

`_ModelBeforeValidator.__call__(value)`:

```python
if value is None and self.allow_none:
    return None
if isinstance(value, self.model):
    return value
raise ValueError(
    f"{self.model.__name__} field must be resolved to an instance before "
    f"validation (got {value!r}); djhtmx resolves Model fields during build."
)
```

No ORM. No lazy branch. Remove `_get_instance`, `_get_lazy_proxy`,
`_LazyModelProxy` (whole class), and `ModelConfig.lazy`. `_Model` always uses
`base_type = model` (no proxy type). `is_simple_annotation` drops the
`_LazyModelProxy` origin check.

`_QuerySet` validator stays as-is — `filter(pk__in=v)` is lazy (no query at
validation), so it already satisfies "no DB in the validator". (QuerySet
*iteration* on the loop in an async handler remains a separate, documented
caveat — out of scope here.)

### 2. Resolvers (`introspection.py`)

Reuse `_model_field_resolvers(component_class)` (already added: reads each
field's `_ModelBeforeValidator` off its metadata).

Two resolution functions, both producing **instances** in a copy of `state`:

```python
def resolve_model_fields(cls, state) -> state          # sync: .filter(pk=pk).first()
async def aresolve_model_fields(cls, state) -> state    # async: await ….afirst()
```

Each, per Model field present in `state` whose value is **not** already an
instance and not `None`:
- builds `M.objects` with `select_related` / `prefetch_related` from the
  field's `ModelConfig`,
- fetches by pk (`first()` / `await afirst()`),
- on **not found**: `None` if `allow_none` else raise (same rule the validator
  would have enforced).

`select_related` works with `afirst`. **`prefetch_related` + `afirst` needs
verification** (prefetch is applied on queryset evaluation; if `afirst` doesn't
trigger it, fall back to `async for obj in qs[:1]` or
`aprefetch_related_objects`). → **open item**.

### 3. Build (`repo.py`)

`_build_from_state` no longer resolves Models; it assumes `state` already holds
instances (validators are pure). Resolution happens in the callers, *after*
query-patchers, *before* construction:

- **sync `build`** (template render path): `state = resolve_model_fields(cls, state)` (sync ORM, on the sync render thread).
- **async `abuild`**: `state = await aresolve_model_fields(cls, state)` (async ORM, on the loop), then construct on the loop. **No `submit_sync_work` for the build** — model fetch is async, construction is pure CPU. The `eager_models` flag is removed.

Patcher ordering: patchers run first (they add query-sourced pks to `state`),
then resolution turns every Model pk — state-sourced or query-sourced — into an
instance.

### 4. Query fields — the entangled part (`query.py`)

Today the patcher resolves Model query fields through the validator/adapter.
With pure validators it must stop doing that:

- `QueryPatcher` learns whether its field is **Model-typed** (via the same
  `_ModelBeforeValidator` lookup) and, if so, carries a **pk adapter** (the
  pk's scalar type, e.g. `UUID`/`int`) instead of the full Model adapter.
- `get_update_for_state` for a Model field parses/validates only the **pk**
  from the URL (preserving the "ignore ill-formed query value → keep default"
  behaviour for unparseable pks) and writes the **pk** into `state`.
- The instance is then loaded by `(a)resolve_model_fields` during build, exactly
  like a state-sourced Model field.
- **Not-found semantics**: a syntactically-valid pk that matches no row must
  fall back to the field default (current behaviour), not raise. So resolution
  needs to know a field is "query-defaulted". Options to settle in
  implementation: (a) resolver returns `None`/default for not-found on
  `allow_none` fields and the patcher only sets pks for `allow_none`/defaulted
  fields; (b) the patcher pre-checks existence. → **open item** (this is the
  subtlest behavioural corner).

Non-Model query fields (int/str/enum/date/collections) are unchanged — their
adapters never touched the ORM.

## Connection-model impact

- Build-time Model fetches now use **Django's async-ORM connection**
  (thread-sensitive, shared, serialized) — a new DB-connection source distinct
  from the `DJHTMX_SYNC_WORKERS` pool. Bounded (1 shared) but serialized: many
  concurrent builds serialize their model fetches. Document in
  `docs/async-architecture.md`.
- Sync render path: unchanged (sync ORM on the render thread).

## Breaking changes & test migration

- **Removed public API**: `ModelConfig.lazy`, `_LazyModelProxy`. Any component
  using `Annotated[Model, ModelConfig(lazy=True)]` must drop it (fields are
  always eager now).
- **CHANGELOG**: breaking entry under 2.0; document the connection-model note.
- **Tests to rewrite/remove**:
  - `test_introspection.py` lazy tests (~lines 381–476) — delete or convert to
    eager expectations.
  - `test_model_resolution.py` — drop the `lazy=True` probe; assert async build
    resolves via `afirst` and that the validator **raises** on a bare pk.
  - Any Model **query-field** tests — verify URL pk → instance still works via
    the new pk-adapter + build resolution path, including malformed-pk and
    not-found fallbacks.

## Risks / open items

1. **`prefetch_related` + `afirst`** correctness (see §2). Highest-risk unknown.
2. **Query-field not-found/default semantics** (see §4). Behaviourally subtle;
   needs explicit tests for: missing param, malformed pk, valid-but-absent pk,
   present-and-valid pk.
3. **Validator strictness** ("let it fail"): any build path that forgets to
   pre-resolve now raises instead of silently querying. We must confirm *every*
   construction path goes through `resolve_model_fields`/`aresolve_model_fields`
   (sync `build`, `abuild`, and — importantly — `model_validate`/`model_validate_json`
   if used anywhere on raw state). Grep for direct `REGISTRY[name](...)` /
   `model_validate` outside the build helpers.
4. **Serialization round-trip**: `model_dump_json` still emits pk; confirm no
   path reconstructs a component straight from dumped JSON without going through
   a resolver.
5. **WebSocket consumer** build path (`from_websocket` + dispatch) must use the
   async resolver too.

## Sequencing (when approved)

1. Pure validator + remove lazy (introspection) + fix `is_simple_annotation`.
2. `resolve_model_fields` / `aresolve_model_fields` (sync uses `first()`, async
   uses `afirst()`); resolve prefetch handling.
3. `repo`: sync `build` resolves sync; `abuild` resolves async on the loop; drop
   `eager_models`.
4. `query.py`: Model query fields → pk adapter + build-time resolution; settle
   not-found semantics.
5. Rewrite tests; CHANGELOG; docs/async-architecture note.
6. Full suite + zuban + a query-field matrix of edge cases.
