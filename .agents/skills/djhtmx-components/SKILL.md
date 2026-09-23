---
name: djhtmx-components
description: "Read this before writing or changing a subclass of HtmxComponent: what belongs in the state, what a handler takes, how a component reports a failure, and the checks to run afterwards."
---

## Overview

djhtmx will discover all modules `htmx.py` in all your apps and registers all components found there.  It also discovers all modules inside packages `htmx/` in your apps.  Split into packages when the amount of components is large.

You declare HTMX components by:

- Inheriting from `HtmxComponent` and setting a `_template_name`.
- `HtmxComponent` is a pydantic BaseModel.  Put all required state as annotations of the class definition and they will be stored.
- Interactions hydrate the state from a Redis DB.  Keep your state as small as workable.  See `DJHTMX_KEY_SIZE_WARN_THRESHOLD`.
- Properties (and `cached_property`) in the component which don't start with `_` are lazily exposed to the template as context; and they are all cached during  the render.  The difference between a `property` and a `cached_property` is that the second is cached for Python as well.

Read the `djhtmx-testing` skill before writing the tests.

## The modules a component needs

- `djhtmx.component` -- `HtmxComponent`, `Query`, `ModelConfig`, `annotated_handler`, `is_usable_user`, `requires_logged_user`.
- `djhtmx.commands` -- `Render`, `SkipRender`, `BuildAndRender`, `Destroy`, `Emit`, `Redirect`, `Open`, `Focus`, `ScrollIntoView`, `PushURL`, `ReplaceURL`, `Execute`, and `DispatchDOMEvent`.
- `djhtmx.global_events` -- `HtmxUnhandledError`.
- `djhtmx.exceptions` -- `ComponentNotFound`, `LoginRequired`.
- `djhtmx.sse` -- `SSESubscription`, `SSEEventEnvelope`, `emit_sse_event`.
- `djhtmx.utils.subscriptions` -- `get_instance_subscriptions`, `get_model_subscriptions`.
- `djhtmx.utils` -- `run_on_commit`.

Import a public name from the module that documents it.

## Reference

Read the file for what you are about to write:

- [basic](references/basic.md) -- declaring a component, what belongs in the state, handlers and the parameters they receive, the default render and `SkipRender`.
- [authentication](references/authentication.md) -- the `user` field, and making a component refuse to exist without a logged-in one.
- [model-prefetching](references/model-prefetching.md) -- `ModelConfig(prefetch_related=...)` and `select_related`, for a render that walks relations.
- [model-lazyness](references/model-lazyness.md) -- `ModelConfig(lazy=True)`, for a row most requests never read.
- [query-string](references/query-string.md) -- `Query`, for the state that belongs in the URL and the components that share it.
- [events](references/events.md) -- `Emit` and `_handle_event` between the components of one page, and `Execute`.
- [model-subscriptions](references/model-subscriptions.md) -- redrawing when a row somebody else writes changes.
- [sse-events](references/sse-events.md) -- events that cross processes and sessions, and what fails silently about them.
- [partial-rendering](references/partial-rendering.md) -- rendering one region with `Render(self, template=...)` and `{% oob %}`.
- [child-components](references/child-components.md) -- `BuildAndRender`, `parent_id` and `Destroy`.
- [lazy-components](references/lazy-components.md) -- placing a component with `lazy=True`, and the placeholder it renders meanwhile.
- [dom-interactions](references/dom-interactions.md) -- `Redirect`, `Open`, `Focus`, `ScrollIntoView`, `PushURL`/`ReplaceURL`, `DispatchDOMEvent`.
- [errors](references/errors.md) -- what djhtmx does with a handler that raises, and what a component should say itself.

Run the project's registry checks (`make check-htmx`, or `python manage.py htmx check-missing`/`check-shadowing`/`check-unused-non-public`) after you add, rename or remove a component.
