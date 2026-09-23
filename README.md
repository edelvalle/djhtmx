# djhtmx

[![CI](https://github.com/edelvalle/djhtmx/actions/workflows/ci.yml/badge.svg)](https://github.com/edelvalle/djhtmx/actions/workflows/ci.yml)

Interactive UI Components for Django using [htmx](https://htmx.org)

## Agent Skills

This repository is the home of three skills for coding agents: `djhtmx-setup` for wiring djhtmx into a project and its settings, `djhtmx-components` for writing components, and `djhtmx-testing` for testing them.  Install them in your project with [dotagents](https://github.com/getsentry/dotagents), so the guidance travels with the library instead of drifting in each consumer:

```toml
[[skills]]
name = "djhtmx-setup"
source = "edelvalle/djhtmx"

[[skills]]
name = "djhtmx-components"
source = "edelvalle/djhtmx"

[[skills]]
name = "djhtmx-testing"
source = "edelvalle/djhtmx"
```

They describe djhtmx alone; the conventions of your own project belong in a skill of your own that points at these.

## Install

```bash
uv add djhtmx
```

or

```bash
pip install djhtmx
```

# Configuration

djhtmx needs **Redis** running for component state, and five things wired:

```python
INSTALLED_APPS = [
    ...,
    "djhtmx",
]

# Last, so its exception hook runs first.
MIDDLEWARE = [
    ...,
    "djhtmx.middleware",
]

TEMPLATES = [{
    ...,
    "OPTIONS": {"context_processors": [
        ...,
        "django.template.context_processors.request",
        "djhtmx.context.component_repo",
    ]},
}]
```

```python
# urls.py -- the prefix is yours
urlpatterns = [
    ...,
    path("_htmx/", include("djhtmx.urls")),
]
```

```html
{# the base template #}
{% load htmx %}
<!doctype html>
<html>
  <head>
    {% htmx-headers %}
  </head>
</html>
```

`DJHTMX_REDIS_URL` defaults to `redis://localhost/0`.  Every setting, what running server-sent events asks of the deployment, and what to look at when a component renders but does not respond are in the `djhtmx-setup` skill.

## Getting started

djhtmx is a framework for building components, not a component library: it ships no components, templates or styles, and is unopinionated about all three.

Components are discovered in the `htmx` module -- or the `htmx` package -- of each installed app.

```python
from djhtmx.component import HtmxComponent


class Counter(HtmxComponent):
    _template_name = "Counter.html"
    counter: int = 0

    def inc(self, amount: int = 1):
        self.counter += amount
```

The `inc` event handler is ready to be called from the front-end to respond to an event.

The `counter.html` would be:

```html
{% load htmx %}
<div {% hx-tag %}>
  {{ counter }}
  <button {% on "inc" %}>+</button>
  <button {% on "inc" amount=2 %}>+2</button>
</div>
```

When the event is dispatched to the back-end the component state is reconstructed, the event handler called and then the full component is rendered back to the front-end.

Now use the component in any of your html templates, by passing attributes that are part of the component state:

```html
{% load htmx %}

Counters: <br />
{% htmx "Counter" %} Counter with init value 3:<br />
{% htmx "Counter" counter=3 %}
```

The `djhtmx-components` skill covers the rest -- state and model fields, handlers, events, model subscriptions, query-string state, rendering, SSE -- and `djhtmx-testing` covers testing components with `djhtmx.testing.Htmx`.
