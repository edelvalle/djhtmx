# Lazy components

A component placed with `lazy=True` renders a placeholder instead of itself:

```html
{% htmx "ExpensiveReport" owner=owner lazy=True %}
```

What lands on the page is a small "Loading..." element carrying the component's id, with `hx-trigger="revealed"` (plus a random delay of 100-1000ms) and a `hx-get` to the component's `render` handler.  The real render is fetched when the element scrolls into view, so the page answers while the expensive part arrives on its own.

Use it for a component whose render is slow and whose content nobody needs immediately: a report below the fold, a panel in a tab that is not open, a count that walks a large table.  A component that is cheap gains nothing and costs a second request.

## The placeholder is a template of its own

`_template_name_lazy` names it, and it defaults to djhtmx's `htmx/lazy.html`, which is the word "Loading...".  Give the component its own whenever the real render has a size, so the page does not jump when the content arrives:

```python
class ExpensiveReport(HtmxComponent):
    _template_name = "ExpensiveReport.html"
    _template_name_lazy = "ExpensiveReport_loading.html"
    owner: Owner
```

```html
{# ExpensiveReport_loading.html #}
{% load htmx %}
<div {% hx-tag %} class="report skeleton" aria-busy="true">
  <div class="skeleton-row"></div>
  <div class="skeleton-row"></div>
</div>
```

The placeholder **must** carry `{% hx-tag %}`: that is what puts the component's id on it along with the trigger that fetches the real render.  Without it nothing ever loads.

The placeholder is not rendered with the component's context -- the properties and fields the real template reads are not evaluated, which is the whole point of not rendering yet.  `_get_lazy_context` is what it gets instead, for the little the placeholder needs:

```python
def _get_lazy_context(self):
    return {"title": self.owner.name}
```

Keep that cheap.  Anything expensive in there defeats the laziness.

`render` is the handler the placeholder calls.  It does nothing by default -- the component simply renders -- and you override it when the component has work to do the first time it appears:

```python
class ExpensiveReport(HtmxComponent):
    _template_name = "ExpensiveReport.html"
    owner: Owner

    def render(self):
        warm_the_cache(self.owner)
```

## The placeholder comes back

`lazy=True` is part of the component's state, and it decides how a *later* render is drawn:

- A render caused by the component's own handler -- someone clicked something inside it -- is always the real one.
- A render caused by something else -- an emitted event, a signal, an SSE event -- draws the placeholder again, and the browser fetches the component once more when it is revealed.

That second rule is the one that surprises: a lazy component that subscribes to events flickers back to "Loading..." every time one arrives.  Either place it eagerly, or have it yield `SkipRender(self)` for the events it does not need to redraw for.

## Spreading out repeated loads

`add_delay_jitter` puts a random delay on a trigger, so a page holding many of these does not fire them all at once:

```html
<div {% on 'load'|add_delay_jitter:'2000, 30000' 'render' %}></div>
```

The argument is the range in milliseconds, `"100, 1000"` by default.
