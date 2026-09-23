# Commands that act on the browser

These are the commands a handler yields to do something the render cannot: move the page, move the focus, or tell the browser's own code that something happened.  They travel with the response and run after htmx settles the swap.

## Navigating

```python
yield Redirect.to("item-detail", item.id)   # full page navigation
yield Open.to(item.get_absolute_url())      # a new tab
```

`Redirect(url)` and `Open(url)` also take a plain URL; `.to(...)` resolves a view name, a model, or a callable the way `django.shortcuts.resolve_url` does.  `Open` defaults to `target="_blank"` with `rel="noopener noreferrer"`.

A `Redirect` supersedes any URL change in the same response, and it ends the interaction: nothing the handler renders afterwards will be seen.

## Changing the URL without navigating

```python
yield PushURL.to("item-detail", item.id)    # adds a history entry
yield ReplaceURL.from_params(params)        # rewrites the current one
```

`PushURL` when the back button should return to what the page showed before -- a filter applied, a page turned, a record opened.  `ReplaceURL` for state that changes on every keystroke, which would otherwise bury the previous page under a hundred history entries.

A [`Query`](query-string.md) field does this for you: assigning to it rewrites the URL without a command.

## Moving the person's attention

```python
yield Focus(f"#{self.id} input[name=text]")
yield ScrollIntoView(f"#{self.id} .error", block="start", if_not_visible=True)
```

`Focus` after a render that replaced the element someone was typing in -- opening an inline editor, showing a validation error.  The swap destroys the focused node, so the focus has to be asked for again.

`ScrollIntoView` takes `behavior` (`smooth` by default), `block` (`center` by default) and `if_not_visible=True`, which skips the scroll when the element is already in view and spares the person a pointless jolt.

## Talking to the browser's own code

```python
yield DispatchDOMEvent(target=f"#{self.id}", event="report:ready", detail={"rows": len(rows)})
```

It fires a `CustomEvent` that anything listening with `addEventListener` receives, `detail` and all.  This is the bridge to the code djhtmx does not own -- a chart that must redraw, an Alpine component that must close, a web component that keeps its own state.  Pair it with `SkipRender(self)` when that code owns the DOM you would otherwise swap.

`bubbles`, `cancelable` and `composed` are off by default; turn `bubbles` on when the listener sits above the target.
