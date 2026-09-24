# Components, state and handlers

A component is a pydantic model that renders a template and answers the events its template binds.

```python
class Counter(HtmxComponent):
    _template_name = "Counter.html"
    counter: int = 0

    def inc(self, amount: int = 1):
        self.counter += amount
```

```html
{% load htmx %}
<div {% hx-tag %}>
  {{ counter }}
  <button {% on "inc" %}>+</button>
  <button {% on "inc" amount=2 %}>+2</button>
</div>
```

`{% hx-tag %}` goes on the component's single root element and nowhere else: it carries the id djhtmx swaps the render into.  A component whose template has two roots, or whose root is inside an `{% if %}`, loses the half that the tag is not on.

## State

Every annotated field is state, and it round-trips through Redis between requests.  So a field is what the component needs in order to answer the next event, and nothing more -- see [model-prefetching](model-prefetching.md) and [model-lazyness](model-lazyness.md) for what that costs when the field is a row.

Hold rows, not ids.  A field annotated with a model class keeps the pk and gives the row back on the next request, so a component does not carry `owner_id: UUID` behind a property that reads the owner again on every render:

```python
class ItemEditor(HtmxComponent):
    owner: Owner
    opened: Item | None = None
```

The component then compares and filters with the rows it already holds -- `item == self.opened`, `Item.objects.filter(owner=self.owner)`.

Use `Model | None` for a row someone can delete while the page is open: the field reads back as `None` when the row is gone, where a required `Model` raises a `ValidationError`.  A `QuerySet` field is state too, kept as the list of pks, so a big one is paid on every request.

Annotate with `Field(exclude=True)` what the request rebuilds anyway rather than what the session should carry; `user` is the standard case, and [authentication](authentication.md) covers it.

## Properties are the template's context

Every attribute that does not start with `_` reaches the template, and a `property` is evaluated only if the template asks for it, at most once per render.  A `cached_property` also survives within the same Python instance, which matters when a handler and the render that follows it both read the same expensive value.

Put the reading of rows in a property, not in a field, when the answer depends on state the component already holds:

```python
@property
def items(self) -> ItemQS:
    return Item.objects.filter(owner=self.owner).order_by("name")
```

## Handlers

A handler is a plain method whose name says what it does -- `toggle_item`, `open_the_item`, `rebuild_the_items`.  A noun names state, not an action.

Its parameters are validated by pydantic from what the page sent, so annotate the narrowest type you can.

**A model class does not fetch the row here.**  A field annotated with a model class is rewritten so that the stored pk comes back as the row; a handler parameter is not.  `item: Item` means the value must already *be* an `Item`, so the pk the browser sends fails validation before the handler body runs:

```python
def toggle_item(self, item: Item): ...   # raises is_instance_of on the pk the page sends
```

Take the pk, annotated as the model's pk type, and read the row:

```python
def toggle_item(self, item_id: UUID):
    self.item = Item.objects.get(pk=item_id, owner=self.owner)
```

```html
<input type="checkbox" {% on 'change' 'toggle_item' item_id=row.id %}>
```

Scope that read to what the component already holds -- `owner=self.owner` above -- rather than reading by pk alone.  The pk arrives from the page, so the query is where a component decides which rows this person may touch.

Never call a handler from another handler as a plain method when what you want is the whole cycle -- that skips the command processor.  Reach for `Execute` (see [events](events.md)) instead.  An `async def` handler is refused when the component registers: handlers run synchronously, so a coroutine function would never run.

### The parameters the page sends implicitly

`{% on %}` binds an event to a handler.  The trigger is implicit when you name only the handler (`click` for a button, `change` for an input), explicit when you name both: `{% on 'click' 'inc' amount=2 %}`.

Whatever the handler does not receive explicitly is collected from the inputs inside the component that carry a `name`:

```python
def create(self, name: str, is_active: bool = False):
    Item.objects.create(name=name, is_active=is_active)
```

```html
<form {% hx-tag %} {% on "submit" "create" %}>
  <input type="text" name="name">
  <input type="checkbox" name="is_active">
  <button type="submit">Create!</button>
</form>
```

A `name` ending in `[]` arrives as a list, which is how a multiple selection reaches a `QuerySet` parameter, and a dotted `name` builds a nested structure:

```html
<input type="checkbox" name="selected[]" value="{{ item.id }}">
<input type="text" name="address.street">
```

```python
def delete(self, selected: ItemsQS | None = None):
    if selected is not None:
        selected.delete()
```

## Placing a component

```html
{% htmx "ItemEditor" owner=owner %}
{% htmx ItemEditor owner=owner %}
```

Pass the instance the view or the template already holds, not its pk: the pk costs a query to read a row that is already in hand.  The keyword arguments are the component's initial state, and a field the placement does not pass keeps its default -- or comes from the URL, for a [`Query`](query-string.md) field.

## The default render

A handler that yields nothing re-renders its own component, which is what most handlers want.  Yield `SkipRender(self)` when that render is wrong or wasted:

- the handler changed something the component does not show;
- the template's state lives in the browser (Alpine, a chart, an open `<details>`) and a swap would throw it away;
- another command already draws the change -- see [partial-rendering](partial-rendering.md) and [child-components](child-components.md).

Everything else a handler can yield is in [dom-interactions](dom-interactions.md), [events](events.md), [partial-rendering](partial-rendering.md) and [child-components](child-components.md).
