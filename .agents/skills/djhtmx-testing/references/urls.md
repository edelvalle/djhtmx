# The URL a test is on

`Htmx` tracks where the page is, and keeps it in step with what the dispatch does:

- `htmx.path` -- the path with no query string.
- `htmx.query_string` -- the query string with no leading `?`.
- `htmx.url` -- both, without a trailing `?` when there is no query.

## Starting a component from the URL

A `Query` field takes its value from the query string when the placement does not pass one, so the URL is how a test puts a component into the state a shared link would:

```python
self.htmx.navigate_to(f"{self.url}?item={self.first.pk}&showing=active")
editor = self.htmx.get_component_by_type(ItemEditor)
self.assertEqual(editor.opened, self.first)
```

That is also the way to test the reading side of a `Query` without going through a handler at all.

## Asserting that a handler moved the URL

Assigning to a `Query` field rewrites the query string, and the runtime applies that to the test's own URL:

```python
self.htmx.send(editor.open_the_item, item=self.second)
self.assertIn(f"item={self.second.pk}", self.htmx.query_string)
```

`PushURL` and `ReplaceURL` do the same when a handler yields them.  When what the test means is "the handler asked for this URL" rather than "the page ended up there", watch the command with `assertYields(PushURL)` -- see [assertions](assertions.md).

## Redirects navigate for real

A `Redirect` (or an `Open`) yielded inside a dispatch makes the runtime navigate at the end of it: `htmx.dom`, `htmx.path` and the repository all become those of the new page, and the components of the old one are gone.  A test that asserts on the destination page can simply continue:

```python
self.htmx.send(editor.save_and_leave)
[heading] = self.htmx.select("h1")
self.assertEqual(heading.text_content(), "Owner detail")
```

A test that only cares that the handler asked to leave should watch the command instead, and stay where it was.
