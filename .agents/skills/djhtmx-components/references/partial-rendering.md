# Partial rendering

A full render replaces the component's whole DOM subtree.  While someone is typing that costs the caret, the selection, and whatever the browser was holding: the classic symptom is a search box that loses a keystroke every time the list below it updates.

Render only the part that changed.  Split the template, mark the partial's root with `{% oob %}`, and yield a `Render` naming that template:

```python
class SmartFilter(HtmxComponent):
    _template_name = "SmartFilter.html"
    query: str = ""

    @property
    def items(self):
        items = Item.objects.all()
        if self.query:
            items = items.filter(name__icontains=self.query)
        return items

    def filter(self, query: str):
        self.query = query.strip()
        yield Render(self, template="SmartFilter_list.html")
```

```html
{# SmartFilter.html #}
{% load htmx %}
<div {% hx-tag %}>
  <input type="text" name="query" value="{{ query }}" {% on "keyup" "filter" %}>
  {% include "SmartFilter_list.html" %}
</div>
```

```html
{# SmartFilter_list.html #}
<ul {% oob "list" %}>
  {% for item in items %}
    <li>{{ item }}</li>
  {% empty %}
    <li>Nothing found!</li>
  {% endfor %}
</ul>
```

The rules the pieces have to keep:

- The partial has a **single root element** carrying `{% oob "<suffix>" %}`.  The tag gives that element the id `<component-id>-<suffix>`, which is what the out-of-band swap replaces; two roots means the second one is dropped.
- The main template `{% include %}`s the partial, so the first render and the partial render draw the same HTML.
- `Render(self, template=...)` replaces the default full render.  You do not add `SkipRender(self)` next to it.
- Name partials after the component that owns them -- `SmartFilter_list.html` next to `SmartFilter.html` -- so a reader finds both at once.

A component can have several partials, one per region that updates on its own.  When the handler changes something outside all of them, let it render in full instead of yielding one `Render` per partial.

## Rendering with a context of its own

`Render(self, context={...})` renders with that context instead of the component's own.  It is for data that the render needs and the state should not carry -- a computed table, a result that came from elsewhere, something too big to round-trip through Redis:

```python
def show_report(self, kind: ReportKind):
    yield Render(
        self,
        template="Report_table.html",
        context={"rows": build_report(kind), "kind": kind},
    )
```

djhtmx keeps what the template machinery needs (`this`, `htmx_repo`) and the component's state is untouched: nothing of that context survives into the next request, which is the point.  If the next event needs it too, it was state after all.
