---
name: djhtmx-setup
description: "Read this to install djhtmx in a Django project, to change a DJHTMX_* setting, or when a component renders but nothing responds."
---

# Setting up djhtmx

djhtmx needs six things wired, and every one of them fails quietly on its own.  Do them in this order and check the result; [troubleshooting](references/troubleshooting.md) reads the symptoms backwards when something is already broken.

Read the `djhtmx-components` skill to write components, and `djhtmx-testing` to test them.

## Redis

Component state lives in Redis, not in the database or the Django session.  djhtmx connects at import time, so a project without a reachable Redis fails at startup rather than at the first interaction.  Point `DJHTMX_REDIS_URL` at it -- the default is `redis://localhost/0`, which is right only for a laptop.

## The app

```python
INSTALLED_APPS = [
    ...,
    "djhtmx",
]
```

Its `ready()` imports, for every installed app, an `htmx` module or an `htmx` package -- the package recursively.  A component in a module neither of those reaches is never registered: `{% htmx "Name" %}` will not find it, and it gets no URL.

## The middleware, last

```python
MIDDLEWARE = [
    ...,
    "djhtmx.middleware",
]
```

It flushes the session the request touched and answers a `LoginRequired` raised while rendering a page with a redirect to the login page.  **Last** is not decoration: Django calls `process_exception` from the innermost middleware outward, so the last entry is the first to see the exception, before anything outer turns it into a 500.

## The context processor

```python
TEMPLATES = [{
    ...,
    "OPTIONS": {"context_processors": [
        ...,
        "django.template.context_processors.request",
        "djhtmx.context.component_repo",
    ]},
}]
```

`component_repo` is what puts the repository in the template context, and every djhtmx tag reads it from there.  `request` has to be there too -- `{% htmx-headers %}` renders nothing at all without it, which is the quietest failure in this list.

## The URLs

```python
urlpatterns = [
    ...,
    path("_htmx/", include("djhtmx.urls")),
]
```

The prefix is yours.  What the include brings is one URL per registered component plus the SSE endpoint, and it builds that list from the registry **at import time**: a component registered after `djhtmx.urls` is imported has no URL, whatever the template says.

## The headers

```html
{% load htmx %}
<!doctype html>
<html>
  <head>
    {% htmx-headers %}
  </head>
</html>
```

This loads htmx itself, the SSE extension and djhtmx's own script, and it carries the CSRF header name.  Without it the page renders correctly and does nothing at all when clicked -- there is no error anywhere.

## The SSE router, if the project uses SSE

```html
{% htmx "SSEEventRouter" %}
```

Once per page, normally in the base template.  It opens the single SSE connection and routes what arrives into the DOM.  SSE also needs an ASGI server: the endpoint answers `501 SSE requires ASGI` under WSGI.  See [sse-deployment](references/sse-deployment.md).

## Checking it

```bash
python manage.py htmx check-shadowing
python manage.py htmx check-unused-non-public
rg --pcre2 --type html -o --no-heading --no-filename "{% htmx [\"'](\w+)" . \
  | awk '{print $3}' | cut -b2- | sort -u \
  | python manage.py htmx check-missing -
```

`check-shadowing` finds two components answering to the same name, `check-missing` a `{% htmx "Name" %}` that names no component, and `check-unused-non-public` a non-public component nothing subclasses.  Put them behind a `make check-htmx` target and run them after adding, renaming or removing a component.

## Reference

- [settings](references/settings.md) -- every `DJHTMX_*` setting and what changing it costs.
- [sse-deployment](references/sse-deployment.md) -- what SSE asks of the server, and the render pool's database connections.
- [troubleshooting](references/troubleshooting.md) -- symptom to cause, for a setup that is already wrong.
