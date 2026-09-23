# Reading the page

`htmx.dom` is the parsed page, kept up to date: every `send` and `trigger` applies what the dispatch produced to it, the way a browser would.  A full render replaces the component's element by id, an append or a prepend inserts the new fragment into the target, and a `Destroy` removes the element.

```python
[count] = self.htmx.select(".todo-count")
self.assertEqual(count.text_content(), "2 items left")

rows = self.htmx.select('[hx-name="TodoItem"] label')
self.assertEqual([row.text_content() for row in rows], ["First task", "Second task"])
```

- `select(css)` answers with the matching elements.  Unpacking (`[count] = ...`) is the habit worth keeping: it asserts there is exactly one.
- `find_by_text(text)` answers with the elements whose text is exactly that.
- `print(element)` pretty-prints it with colours, for when a test fails and you want to see what was rendered.

`hx-name` is only on the element while `settings.DEBUG` is on, so a selector that uses it depends on the test settings.  Prefer a class or a role the template owns.

## lxml elements are falsy when empty

```python
parent = element.getparent()
if parent is not None:   # never `if parent:`
    ...
```

An element with no children is falsy, so a truth test on an element silently means "has children".  Always compare against `None`.

## What to assert on

Assert on the DOM when the rendered output *is* the behaviour: a row that appears, an empty state, a button that is disabled, the order of a list.

Assert on the component instead when the question is about state -- read it again after the send, as [basic](basic.md) describes.  Reaching into the DOM for something the component knows makes the test depend on the markup, and it will break the next time the template is restyled.

Assert on neither when what you mean is an event or a command: those never reach the DOM at all, and [assertions](assertions.md) covers them.
