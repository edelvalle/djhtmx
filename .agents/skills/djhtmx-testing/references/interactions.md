# Acting on the page

## Send through the bound method

```python
self.htmx.send(editor.open_the_item, item=self.first)
self.htmx.send(editor.toggle_item, item=self.second)
```

`send` runs the handler through the command processor and applies everything it produced -- renders, out-of-band swaps, destroys, URL changes -- and then delivers the session's pending SSE events, exactly as a browser would between two requests.

Two rules it enforces or rewards:

- **Every argument by name.**  `send` asserts that nothing is positional.
- **The bound method, not a string.**  `editor.toggle_item` is a real reference, so a renamed handler or a changed signature is a type error at `make typecheck` rather than a failure three minutes into a test run.

Pass what the handler declares.  A handler takes the pk of a row rather than the row itself -- see the `djhtmx-components` skill -- so pass the pk, and pass an instance only where the parameter is annotated with the model class.

`dispatch_event(component_id, "handler_name", {...})` does the same from an id and a string.  It is the escape hatch for a component the test has no reference to -- one built by another component's handler, say -- and it checks nothing, so prefer `send` everywhere else.

## Never call the handler yourself

```python
editor.toggle_item(item.pk)   # not a test of anything
```

That call skips the command processor: the arguments are never coerced, the commands it yields are never processed, the new state is never stored, and nothing renders.  What remains is a method call on a detached object -- the machinery the test exists to exercise is precisely what it stepped over.

## Going through the template

`send` starts from the component.  To cover the binding in the template -- that the button is wired to the handler you think, with the arguments you think -- act on the DOM instead:

```python
self.htmx.type_into("input.new-todo", "3rd task")
self.htmx.trigger("input.new-todo")
```

`type_into(selector, text)` appends to what the element holds, or replaces it with `clear=True`.  It works on a text input or a textarea.

`trigger(selector)` fires what the element is bound to.  It reads the handler off the element's `hx-post`, so the element must be one `{% on %}` bound; it toggles a checkbox and selects a radio the way a click would; and it gathers the values the request would carry -- the `hx-vals` the tag wrote, and every named input matched by `hx-include`.  That last part is what makes it the only way to test [implicit parameters](../../djhtmx-components/references/basic.md) end to end.

Both take a selector or an element you already found with `select`.

A test that uses `trigger` is testing the template as well as the handler, and costs a CSS selector that breaks when the markup changes.  Use it where the binding is the thing under test, and `send` for everything else.
