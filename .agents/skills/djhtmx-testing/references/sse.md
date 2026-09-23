# Testing SSE events

The test runtime delivers SSE events the way the browser receives them between requests: `send`, `trigger` and `dispatch_event` all end by draining the session's pending events and applying whatever they rendered.  So a component that reacts to an event caused by a handler needs nothing special -- the reaction is already in `htmx.dom` when the send returns.

An event raised by something else -- the test body saving a model, a service function called directly -- has nothing to ride on.  Drain it by hand:

```python
Notification.objects.create(user=self.user, body="Something happened")
self.htmx.drain_sse_events()
[toast] = self.htmx.select(".toast")
```

`run_on_commit` runs immediately under tests, so an emitter wired to `post_save` fires inside the test's transaction instead of never.

## Asserting on what the event produced

Emit and drain inside the watching block, and the commands the SSE handler yielded are captured like any others:

```python
with self.htmx.assertYields(BuildAndRender) as commands:
    emit_sse_event(TodoItemAdded(item_id=item.pk), topics={"todo.item"})
    self.htmx.drain_sse_events()
[build] = commands
self.assertEqual(build.state["item"], item)
```

## The test body is not the session

An event emitted from inside a dispatch carries the session that caused it, which is what a handler compares against `self.session_id` to avoid redrawing its own work.  An event emitted from the test body carries no session at all, so the component treats it as coming from elsewhere -- the other branch.

Both branches are worth a test, and they are reached differently:

- **From elsewhere**: emit in the test body, then drain.
- **From this page**: `send` the handler that causes the emission, and let the send drain.

A test that only ever emits from its own body never exercises the guard, and the duplicate render it exists to prevent shows up in a browser instead.

## When nothing arrives

Nothing about a missing SSE subscription raises.  A component that declares `sse_subscriptions` without `_handle_sse_events` (or the reverse) receives nothing and logs a warning, and `emit_sse_event` drops an event whose type no handler annotates.  A test that sees no reaction is therefore as likely to have found a silent misdeclaration as a logic bug -- check both members of the pair before reading the handler's body.
