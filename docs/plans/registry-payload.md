# A richer `REGISTRY` payload

## Goal

Have `REGISTRY` carry what is known about a component rather than only its class, starting with the shape of each of its event handlers, and use that to reject at import a handler shape djhtmx cannot run.

Self-contained: it changes no dispatch behaviour and does not depend on the async-handler work.  It exists partly to keep that work small, and partly because the shape information is already needed today and is currently unavailable.

## Why the shape cannot be read at dispatch time

`component.py:177-182` wraps every parameterised handler in `validate_call`, which is what coerces its arguments.  The wrapper is an ordinary function, so `isgeneratorfunction` and `isasyncgenfunction` answer about the wrapper and not about the handler.  Measured on the nine-component test application: **four components have a handler whose live shape is reported wrongly**.

The shape itself is not ambiguous -- a `yield` anywhere in a body sets `CO_GENERATOR` or `CO_ASYNC_GENERATOR` on the code object at compile time, whether or not that `yield` is reachable.  It is only the wrapper that hides it.  So the shape has to be read **before** the wrapping loop, and the natural place is where the class registers: `REGISTRY[component_name] = cls` at `component.py:140` already runs earlier than the wrapping at `:177`.

## What today's code does with a handler it cannot run

`command_processor.py:111` calls `handler(**handler_kwargs)` and hands the result to `_process_emitted_commands`, which iterates it.  An `async def` handler therefore returns a coroutine that is iterated, raising `TypeError: 'coroutine' object is not iterable`, which the enclosing `except Exception` turns into `Emit(HtmxUnhandledError(...))` plus a default render.  The handler body never runs and the interaction looks like a silent no-op.

Recording the shape lets that become an error at import, naming the component and the handler.

## Design

```python
type HandlerKind = Literal["function", "generator"]

@dataclass(slots=True, frozen=True)
class RegisteredComponent:
    htmx_component_class: type[HtmxComponent]
    handler_kind_mapping: Mapping[str, HandlerKind]

REGISTRY: dict[str, RegisteredComponent] = {}
```

`HandlerKind` lists only the shapes djhtmx runs today.  `coroutine` and `async_generator` are deliberately absent: a handler of either shape is refused at registration with an error naming it, rather than admitted into a mapping the dispatcher has no branch for.  Adding them is the async-handler work's job, and having to add them is exactly the reminder that dispatch must learn to run them.

Coverage: every name `__own_event_handlers(get_parent_ones=True)` yields, plus `_handle_event` and `_handle_sse_events`, which that helper skips because it ignores names beginning with `_` yet which reach the dispatcher by the same path.

`frozen=True` is shallow, so the mapping is annotated `Mapping` to say it is not to be mutated after construction.

## Call sites

Six, all inside djhtmx, all reading the class out of the value: `component.py:68` (`QueryPatcher.for_component`), `component.py:135-137` (the shadowing check, which passes the value to `get_fqn`), `repo.py` (component construction), `urls.py` (route building), and `management/commands/htmx.py` twice.  The sites that only iterate keys are unaffected.

`REGISTRY` is internal; consumers outside djhtmx are not a constraint on its shape.

## Order of work

1. `RegisteredComponent`, `HandlerKind`, and the kind-reading helper; build the mapping at the existing registration point; update the six call sites.  No behaviour change.
2. Refuse `coroutine` and `async_generator` handlers at registration, with a message naming the component and handler.  This is a behaviour change: it converts a silent no-op into an import error.  Worth checking the consuming application for an `async def` handler that is currently dead before this lands.
3. CHANGELOG.

## Verification

Step 1 is covered by the existing suite continuing to pass -- it is a pure refactor.  Step 2 needs a component declaring an `async def` handler to be refused; whether that becomes a test is a decision for whoever implements it, not an assumption of this plan.
