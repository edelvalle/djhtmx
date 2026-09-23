# Asserting on what never reaches the DOM

An event a component emits and a command a handler yields are not HTML.  The toast a `FeedbackMessage` becomes is drawn by JavaScript from a DOM event the test client drops; a `Redirect` is a command, not a page.  To see either, watch the dispatch.

```python
with self.htmx.assertEmits(FeedbackMessage) as captured:
    self.htmx.send(editor.rebuild_the_items)
event = captured.get_event()
self.assertIn("Open an item first", event.body)
```

```python
with self.htmx.assertYields(Redirect) as commands:
    self.htmx.send(editor.save_and_leave)
[redirect] = commands
self.assertEqual(redirect.url, self.owner_url)
```

`assertEmits(EventClass)` watches the events emitted inside the block; `assertYields(CommandClass)` watches the commands the handlers yielded.  Both fail at the end of the block when nothing of that class appeared, and both name what *did* appear -- including which handler yielded each command, which is usually enough to see what went wrong without reading the code.

## Assert after the block, not inside it

What the block receives is a live view, not a snapshot: it is handed over before the first event exists and keeps filling while the block runs.  The assertion that anything appeared at all runs when the block exits.  So `[redirect] = commands` inside the block unpacks an empty list and fails with a confusing `ValueError` instead of the message the assertion would have given.

`captured.get_event()` answers with the first event, and fails naming everything that was emitted -- something `captured[0]` cannot do.

## Watching the whole cascade

A block watches the dispatch, not one call: an event raised by a listener that the first handler woke counts too.  That is what you want when testing that an interaction reaches a component two steps away, and what to remember when an assertion passes for a reason you did not intend.

Several watchers can be open at once, sharing one recording:

```python
with self.htmx.assertYields(Redirect) as commands, self.htmx.assertEmits(Saved) as captured:
    self.htmx.send(editor.save_and_leave)
```

To reach *every* event a block emitted rather than one class, watch `Emit` with `assertYields`.

## Asserting the negative

```python
with self.htmx.assertYields(None):
    self.htmx.send(editor.open_the_item, item=self.item)
```

`assertYields(None)` says the handlers yielded nothing of their own.  The default `Render` djhtmx adds for a handler that yielded nothing is djhtmx's command, not the handler's, so it does not make this fail.

## Looking instead of stating

`capturing(*command_classes)` is the same recorder without the assertion, for a test that asks what a dispatch produced rather than stating up front what it must produce:

```python
with self.htmx.capturing(SkipRender, Emit) as captured:
    self.htmx.send(editor.save)
[skip_render, emit] = captured
```

Called with no class it captures every kind of command.  An empty capture means either that the handlers yielded nothing or that they yielded only other classes; `captured.did_yield` tells those apart, and `captured.describe()` prints everything they yielded.

The order is the order the handlers yielded in, which is not the order the processor ran them: processing an `Emit` can produce further commands, and the queue reorders.  Assert on what is in the capture, not on where it sits.
