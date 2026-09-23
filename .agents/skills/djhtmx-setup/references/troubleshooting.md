# When the setup is wrong

Almost every way of wiring djhtmx badly produces silence rather than an error.  Read from the symptom.

## The page renders, and clicking does nothing

Nothing in the console, no request in the network tab.  In order of likelihood:

1. **`{% htmx-headers %}` is missing** from the base template, so htmx itself was never loaded.
2. **`django.template.context_processors.request` is missing.**  The tag renders *nothing at all* without a request in the context -- the page looks right and no script is loaded.
3. **`djhtmx.urls` is not included**, so there is nowhere to post to.

## A request goes out and comes back 400 "Missing header HX-Session"

The element that was clicked is inside a component whose root element has no `{% hx-tag %}`.  That tag carries the session header; without it the endpoint refuses the request.  Check that the component's template has exactly one root element and that the tag is on it -- not on a child, and not inside an `{% if %}` that was false.

## `{% htmx "Name" %}` renders nothing, or `check-missing` reports it

The component was never registered.  djhtmx imports, per installed app, an `htmx` module or an `htmx` package; a component defined anywhere else -- `views.py`, a module imported lazily -- is invisible.  Move it, or import it from the app's `htmx` module.

The same cause produces a component with no URL, because `djhtmx.urls` builds one URL per component **at the time it is imported**.  A component registered after that point has a template tag that renders and a handler nothing can reach.

## `TypeError: Component X would shadow existing Y` at startup

Two classes share a name, and the registry is keyed by the bare class name across the whole project.  Rename one, or mark the one that is only a base with `public=False`.  `python manage.py htmx check-shadowing` lists them.

## A visitor is sent to the login page unexpectedly

A component on that page annotates a non-optional `user`, which *is* a login requirement.  The middleware logs which component it was, with the path.

A login page that itself mounts such a component redirects to itself forever.  That is the loop to look for when a login page stops loading.

## Nothing arrives over SSE

Work down the list; every step fails silently on its own:

1. **`{% htmx "SSEEventRouter" %}` is not on the page.**  No router, no connection.
2. **The server is WSGI.**  The endpoint answers `501 SSE requires ASGI`; the browser retries forever and the page never updates.
3. **The component declares only one of the pair.**  `sse_subscriptions` without `_handle_sse_events`, or the reverse, logs a warning and subscribes to nothing.
4. **The handler's annotation does not accept the event type**, so the subscription naming it is dropped -- another warning.
5. **Nothing annotates the event type at all**, and `emit_sse_event` returns without publishing.
6. **The emission was not wrapped in `run_on_commit`**, so a worker read the row before the transaction that wrote it committed, and rendered the old state.
7. **A proxy is buffering**, and everything arrives at once, late, or when the page closes.

## Events arrive twice, or a row appears twice

The page that caused the change drew it, and then drew it again when its own event came back.  The handler has to compare `envelope.source_session_id` against `self.session_id` and skip the work it already did.

## Warnings about session key size

A component's state is over `DJHTMX_KEY_SIZE_WARN_THRESHOLD` (50 KB by default) and the warning names it.  State round-trips through Redis on every interaction, so this is a real cost on every click.  Move the data into a property that reads it, and keep in the state only what is needed to read it again.

## A lazy component is stuck on its placeholder

Its `_template_name_lazy` template has no `{% hx-tag %}`, so the element carries neither the id nor the trigger that fetches the real render.

## Everything worked, then the page went stale after a deploy

Component state lives in Redis under the session; flushing Redis, or pointing `DJHTMX_REDIS_URL` at a different database, drops it.  Open pages then rebuild their components from defaults on the next interaction.  The same happens on its own after `DJHTMX_SESSION_TTL`.
