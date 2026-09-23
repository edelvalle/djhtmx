# Settings

Every setting is read once, at import time, from `django.conf.settings`.  Changing one means a restart, and a value that is wrong raises at startup rather than at the first request -- which is the good case.

## Connection and session

**`DJHTMX_REDIS_URL`** (`"redis://localhost/0"`) -- where component state lives.  The connection is built when `djhtmx.settings` is imported, so an unreachable Redis breaks startup.

**`DJHTMX_SESSION_TTL`** (`3600`) -- how long a session's state survives in Redis, in seconds or as a `timedelta`.  This is the real limit on how long a page can sit open and still work: past it, the state is gone and the next interaction rebuilds components from nothing.  Raising it keeps more state alive at once, which costs Redis memory in proportion to the pages open.

**`DJHTMX_SESSION_REFRESH_RATE`** (`0.5`, between 0 and 1) -- when an open SSE connection renews the TTL, as a fraction of it.  At the default a page refreshes its state halfway through the TTL, so a page that stays open stays alive.  Lower means more frequent renewals and more Redis writes; `0` disables the renewal, and then even a connected page expires on schedule.

## Rendering

**`DJHTMX_DEFAULT_LAZY_TEMPLATE`** (`"htmx/lazy.html"`) -- the placeholder for a component placed with `lazy=True` that does not name its own `_template_name_lazy`.  djhtmx's own is the word "Loading...".  Point it at a skeleton of your own to set the project's default; whatever you write must carry `{% hx-tag %}` or nothing ever loads.

## Strictness

**`DJHTMX_STRICT_PUBLIC_BASE`** (`False`) -- djhtmx treats a class named `Base*`, `Abstract*`, `_Base*` or `_Abstract*` as non-public and only logs that it did.  Turning this on makes the guess an error instead, so a base class has to say `public=False` itself.  Worth it in a project where a base accidentally registering as a component has bitten you.

**`DJHTMX_STRICT_EVENT_HANDLER_CONSISTENCY_CHECK`** (`False`) -- checks that a handler overridden in a subclass keeps the signature it had.  On, a mismatch raises at import; off, it is a log line.

## State size

State round-trips through Redis on every interaction, so a component carrying a large field makes every click slower.  These three watch for it:

**`DJHTMX_KEY_SIZE_WARN_THRESHOLD`** (`51200`, 50 KB) -- a state bigger than this logs a warning naming the component.  That warning is the signal to move data out of the state and into a property that reads it.

**`DJHTMX_KEY_SIZE_ERROR_THRESHOLD`** (`0`, disabled) -- the size at which it is logged as an error instead.  Set it in a project where oversized state has caused an incident and you want it to be loud.

**`DJHTMX_KEY_SIZE_SAMPLE_PROB`** (`0.1`) -- the fraction of writes that get measured, because measuring costs.  Raise it while hunting a specific component, lower it if the check itself shows up in a profile.

## Tracing

**`DJHTMX_ENABLE_SENTRY_TRACING`** (`True`) -- spans around dispatch and render, and htmx headers as tags, when Sentry is installed.  On by default, and inert without the SDK.

**`DJHTMX_ENABLE_LOGFIRE_TRACING`** (`False`) -- the same through Logfire.  Turn on exactly one of the two: both enabled means every span is reported twice.

## SSE

The six `DJHTMX_SSE_*` settings are a deployment decision with a database-connection budget attached, and they live in [sse-deployment](sse-deployment.md).
