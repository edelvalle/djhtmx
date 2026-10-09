# Whole-dispatch atomicity for synchronous entry points

## Goal

When the **entry-point handler is synchronous** and a database has `ATOMIC_REQUESTS`, the whole dispatch — component build, every handler reached through `Emit`, and the render — must commit or roll back as a single unit on a single connection, exactly as it did in 1.x.

2.0 as currently built gives one transaction *per handler*, so a cascade is decomposed into several transactions with commits interleaved between them.  A handler that writes and then emits has already committed by the time its listeners run, and a failure downstream leaves that write standing.  This is a silent behavioural change for any application relying on `ATOMIC_REQUESTS`, and it is the one thing blocking the release.

## Non-goals

- **Async entry points.**  A dispatch entered through an async generator streams over an unbounded period; holding a transaction for its duration is worse than not having one.  Those keep per-handler atomicity.
- **SSE drains.**  They were never atomic: 1.x carried `@transaction.non_atomic_requests` on `sse_endpoint`.  Wrapping them would be new behaviour, not a restoration, and is out of scope here.
- **Deferring `emit_sse_event` to commit.**  The publish was never inside the transaction in any version, and `emit_sse_event` has no callers inside djhtmx — every call site is application code.  Whether a given handler wants `run_on_commit` semantics is an application decision, not a framework one.

## What the design rests on

An async handler reached from a synchronous dispatch runs under `async_to_sync`, and its **thread-sensitive ORM work is routed back to the calling thread**, onto that thread's connection, inside that thread's open atomic block.  This was asserted in the docstring at `command_processor.py:322-325` but never demonstrated; it now has been, on SQLite:

| observation | result |
| --- | --- |
| coroutine's own Python frame | runs on a **separate** event-loop thread, whose `connection.connection` is `None` |
| `sync_to_async(…, thread_sensitive=True)` work (what `acreate`/`asave` use) | runs on the **calling** thread, on its connection, with `in_atomic_block` true |
| row written by `await Item.objects.acreate(…)` | visible inside the outer transaction, **gone after its rollback** |

So an async listener woken by a sync handler already joins the caller's transaction; no special handling is needed to keep it there.

The caveat to document: this holds for ORM access, which goes through Django's thread-sensitive wrappers.  Code in a coroutine frame that touches `connection` **directly**, or that uses `sync_to_async(..., thread_sensitive=False)`, runs on the loop thread against a different, transaction-less connection.  Django raises `SynchronousOnlyOperation` for plain sync ORM in async context, which covers the common mistake, but raw cursor use would escape silently.

## Design

### 1. Decide at the entry point, before opening anything

The shape of every handler is known statically — `yield` sets `CO_GENERATOR` / `CO_ASYNC_GENERATOR` at compile time — and the entry handler is reachable from `REGISTRY[component_name]` without building a component.  The rule:

- kind `async_generator` → **no** dispatch transaction (streaming; keep per-handler atomicity).
- `function`, `generator` or `coroutine` → **wrap the dispatch**.

Cascade shape does not enter into it.  A synchronous pipeline coerces every handler it reaches onto its own thread, so listeners cannot escape the transaction whatever colour they are declared.

Reading the marker off the live attribute is unreliable, because `validate_call` erases `isgeneratorfunction` and `isasyncgenfunction` on any public handler that declares parameters.  Rather than unwrap through `raw_function` at every call site, the kind is recorded once at registration — see *A richer registry* below.

### 2. A richer registry

`REGISTRY` stops mapping a name to a bare class and starts carrying what the dispatcher needs to know about it:

```python
type HandlerKind = Literal["function", "generator", "coroutine", "async_generator"]

@dataclass(slots=True, frozen=True)
class RegisteredComponent:
    htmx_component_class: type[HtmxComponent]
    handler_kind_mapping: Mapping[str, HandlerKind]

REGISTRY: dict[str, RegisteredComponent] = {}
```

The kind of every handler is a compile-time property — a `yield` anywhere in the body sets `CO_GENERATOR` or `CO_ASYNC_GENERATOR` on the code object — so it can be read once and trusted forever.  The only thing that can destroy it is a decorator, and the registration point is already on the right side of the one decorator djhtmx applies: `REGISTRY[component_name] = cls` runs at `component.py:140`, while the `validate_call` loop runs at `component.py:173-191`.  Building the mapping where the class is registered therefore sees every handler unwrapped, and no call site ever needs `raw_function`.

Two points on coverage:

- `__own_event_handlers` (`component.py:214-223`) skips names starting with `_`, so `_handle_event` is not in `_event_handler_params` and must be added to the mapping explicitly.  It is exactly what the listener check in *Async generator in listener position* needs.
- Registration sits inside `if public:`, so the mapping describes public components only.  That matches `LISTENERS`, which is also built under `if public:`, so the two stay consistent.

`frozen=True` is shallow — the annotation is `Mapping` to say the dict is not to be mutated after construction, not to claim it cannot be.

Call sites to update, all inside djhtmx: `component.py:68` (`QueryPatcher.for_component`), `component.py:135-137` (the shadowing check, which passes the value to `get_fqn`), `repo.py:260`, `urls.py:268`, and `management/commands/htmx.py:88,110`.  The ones that only iterate keys (`htmx.py:37,62`) are unaffected.  `REGISTRY` is an internal structure; consumers outside djhtmx are not a constraint on its shape.

Beyond the entry-point decision, this removes the three `inspect` calls `_invoke_handler` performs on **every** handler invocation (`command_processor.py:335-341`) in favour of one dict lookup.  Both call sites already have the name to hand — `event_handler` at `command_processor.py:138` and the literal `"_handle_event"` at `:207`.  `_event_handler_params` is a candidate to fold into the same record later; out of scope here.

### 3. Wrap the submitted callable, never the view

The transaction must open and close on the pool thread that owns the connection.  Wrap inside the job, not around `await submit_sync_work(...)`:

- `urls.py:57` → `_dispatch_request`
- `consumer.py:58` → `Consumer._dispatch`

Gate per alias on `connections[alias].settings_dict["ATOMIC_REQUESTS"]`, so the setting keeps its meaning.  The async views keep `@_non_atomic_for_all_dbs` (`urls.py:28`): that opt-out exists to stop Django's `make_view_atomic` from raising, and is unrelated to the transaction djhtmx opens for itself.

### 4. Suppress the per-handler transaction when a dispatch transaction is open

This is the part that decides the error semantics, and exact 1.x parity is the goal.  1.x behaved as follows, and both halves matter:

- a caught **database** error poisoned the connection, so the next query raised `TransactionManagementError`, which escaped and rolled the request back;
- a caught **non-database** error let the cascade continue and the request **commit**, other handlers' writes included.

Keeping the per-handler `atomic` nested inside the dispatch transaction would turn it into a savepoint, which changes the first case: the failing handler would roll back to its savepoint and everything else would commit.  That is a third behaviour, not 1.x.

So `_drain_sync_handler` (`command_processor.py:353`) opens its own block only when there is no dispatch transaction already open on that alias:

```python
for alias in connections:
    conn = connections[alias]
    if conn.settings_dict["ATOMIC_REQUESTS"] and not conn.in_atomic_block:
        stack.enter_context(transaction.atomic(using=alias))
```

One condition gives both paths: a wrapped cascade gets exactly one flat transaction (1.x parity, including its failure mode), and an unwrapped one — the async-generator entry point — keeps today's per-handler atomicity rather than losing atomicity altogether.

### 5. Async generator in listener position

Under a wrapped cascade this is the one shape that cannot be honoured: `_invoke_handler` would send it to `_drain_async_handler` under `async_to_sync` and drain it to exhaustion *inside* the transaction, holding it open for the length of the stream.

It is detectable at **import** time, not just at dispatch: `LISTENERS` is built in `__init_subclass__` (`component.py:193-197`) and `_handle_event` keeps its markers, so every component that listens to anything can be checked as it registers.  Refuse it there with a clear error, and add a defensive guard in `_invoke_handler` for the dynamic case.

### 6. Documentation

`CHANGELOG.md` currently states, under *Changed*, that "`ATOMIC_REQUESTS` is honoured per sync handler".  That entry describes the behaviour this plan replaces and must be rewritten to say that a synchronous dispatch is atomic as a whole, that a streaming entry point is the documented exception, and that SSE drains are not atomic (as in 1.x).

## Order of work

1. `RegisteredComponent` and the handler-kind mapping, built at `component.py:140`; update the six call sites that read a class out of `REGISTRY`.
2. Entry-point shape resolution: one lookup in the mapping.
3. Dispatch transaction around `_dispatch_request` and `Consumer._dispatch`.
4. The `in_atomic_block` condition in `_drain_sync_handler`.
5. Import-time rejection of async-generator listeners, plus the runtime guard.
6. `_invoke_handler` switches from `inspect` to the mapping.
7. `CHANGELOG.md`.

Step 1 is a pure refactor and lands on its own with no behavioural change.  Steps 2-4 are the behavioural fix.  Steps 5 and 6 are independent of both.

## Verification

The existing suite covers interop (`test_async_cascade.py`), the pool bound (`loadtest_async.py`) and the per-handler wrapping (`test_atomic.py`, which mocks `connections` wholesale).  Nothing covers the properties this plan depends on:

- an async listener woken by a sync handler shares the caller's connection and transaction;
- a cascade with a write, an `Emit`, and a failing listener persists nothing;
- a streaming entry point opens no dispatch transaction.

`test_atomic.py`'s existing assertions will need updating regardless, since `_drain_sync_handler` gains a condition.  **No new tests are written without asking** — flagging the gap for a decision.
