# When a handler fails

djhtmx traps whatever a handler raises -- both at the call and while draining the commands it yields -- logs the traceback, and emits `HtmxUnhandledError`.  The interaction fails cleanly instead of returning a 500 to htmx, which would leave the page looking as if nothing had happened.

So:

- **A handler needs no last-resort `except`.**  One that wraps its whole body to log and swallow only hides the failure from the listener that was going to report it.
- **Subscribe to `HtmxUnhandledError` once, in the application**, with a component that turns it into whatever a person should see for a failure they cannot act on -- typically a toast that says to try again.  That listener is the only place that decides what an unexpected failure looks like.
- **A message the handler writes itself is for a failure the person *can* act on.**  Each such failure gets its own message: one message covering two causes sends the person to the wrong place.

Name the action that failed, so the generic listener can say more than the generic sentence:

```python
@annotated_handler(on_error_message="The items could not be rebuilt. Try again in a few minutes.")
def rebuild_the_items(self): ...
```

The annotations travel with the error and reach the listener as `HtmxUnhandledError.handler_annotations`.  The keys are the application's own -- `on_error_message` is a convention, not something djhtmx reads -- so the listener and the handlers have to agree on them.

Let an exception out of the handler when the failure is a bug: a `ValidationError`, a `DoesNotExist` for a row that should have been there, a broken invariant.  The traceback is what you want in the log, and the person gets the same retry message either way.
