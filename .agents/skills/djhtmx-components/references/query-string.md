# State in the URL

A `Query` annotation ties a field to the page's query string.  The field is read from the URL when the component is placed without it, and assigning to the field rewrites the URL:

```python
class SmartFilter(HtmxComponent):
    _template_name = "SmartFilter.html"
    query: Annotated[str, Query("q")] = ""

    def filter(self, query: str):
        self.query = query.strip()
```

Use it for the state a person would expect to survive a reload or to travel in a shared link: the filter, the selected tab, the row that is open.  Not for state that means nothing outside the moment -- a half-typed form, a menu that is open.

## What the annotation requires

- **A default**, either on the field or through `Field(default_factory=...)`; without one the component raises a `TypeError` when it registers.
- **A type that belongs in a URL**: numbers, strings, `UUID`s, dates, booleans, enums, a model (the pk travels), and collections of those.  Anything else raises a `TypeError`.
- **One `Query` per field.**

## Shared and private parameters

`Query("q")` is shared: every component annotating a field with that same name and type reads and writes the same parameter.  That is the cheapest way to connect two components -- a filter and a list placed side by side need no event between them, because both name `q`:

```python
class SmartFilter(HtmxComponent):
    query: Annotated[str, Query("q")] = ""

class SmartList(HtmxComponent):
    query: Annotated[str, Query("q")] = ""
```

`Query("s", shared=False)` gives the parameter a name of its own per component instance (`s__<ns>=...`), for a component placed several times on one page where each copy holds its own value.

## Subscribing, and opting out

A component is subscribed to its query parameters by default: when anything changes one -- another component, the browser's back button -- the component is woken and rendered.  A component that only ever *sets* the parameter can skip that:

```python
class Selector(HtmxComponent):
    selected_item: Annotated[Item | None, Query("s", auto_subscribe=False)] = None

    def select_item(self, item_id: UUID):
        self.selected_item = Item.objects.filter(pk=item_id).first()
        yield SkipRender(self)
```

Prefer a shared `Query` over an `Emit` and an event class when what travels is a single value that belongs in the URL anyway.  Reach for [events](events.md) when it does not.
