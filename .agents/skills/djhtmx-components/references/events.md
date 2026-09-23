# Events between components

An event is a small object one component emits and others react to, within the same page and the same request.  Use it when components that do not know each other must agree on something that is not in the URL -- reach for a [`Query`](query-string.md) when it is, and for [model subscriptions](model-subscriptions.md) when the news is "this row changed".

```python
@dataclass(slots=True)
class ItemSelected:
    item: Item | None
```

```python
def select(self, item: Item):
    yield Emit(ItemSelected(item))
```

```python
def _handle_event(self, event: ItemSelected):
    match event:
        case ItemSelected(item=item):
            self.selected = item
        case unreachable:
            assert_never(unreachable)
```

djhtmx reads the annotation of `_handle_event` to know what the component listens to, so the annotation is the subscription: a union subscribes to each of its members, and an event class no handler annotates reaches nobody.

## Writing the event

- **Name what happened, in the past tense** -- `ItemSelected`, not `SelectItem` or `ItemSelection`.  An event reports; it does not command.
- **Carry the row, not the id.**  `item: Item | None` costs the emitter nothing and saves every listener a query.
- **Keep it a plain `dataclass`** (`slots=True` is the habit here).  An SSE event is a different thing with a different rule -- see [sse-events](sse-events.md).

## Handling it

`_handle_event` is a handler like any other: it may mutate state, and it may yield commands.  Its default render applies too, so a listener that changed nothing visible yields `SkipRender(self)`.

Match over the event rather than testing it with `isinstance`, and close the match with `assert_never` so a new member of the union is a type error instead of a silent no-op.

The cascade is synchronous and session-local: every listener runs within the dispatch that emitted, and a listener may emit in turn.  Nothing crosses to another browser session -- that is [SSE](sse-events.md).

## Execute

`Execute(component_id, handler_name, data)` runs another component's handler as if the page had posted to it.  It is the tool for "do exactly what that button does" without copying the body of its handler:

```python
yield Execute(self.editor_id, "save", {"text": text})
```

Calling the other component's method directly instead would skip the command processor: the arguments are not coerced, the new state is never stored, and nothing renders.
