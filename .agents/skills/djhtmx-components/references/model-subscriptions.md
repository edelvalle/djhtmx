# Reacting to model changes

A component that must redraw when a row changes -- a row it does not own, and that something else writes -- subscribes to it by name.  Declare the `subscriptions` property; djhtmx wakes the component and renders it when one of those names fires:

```python
@property
def subscriptions(self) -> set[str]:
    if self.user is None:
        return set()
    return get_instance_subscriptions(self.user)
```

`get_instance_subscriptions` and `get_model_subscriptions` from `djhtmx.utils.subscriptions` build the names; both answer with a **set**, so return one directly instead of wrapping it.

The names are:

- `app.model` -- any instance of the model changed
- `app.model.<created|updated|deleted>` -- any instance, one kind of change
- `app.model.<pk>` -- that row changed
- `app.model.<pk>.<created|updated|deleted>` -- that row, one kind of change
- `app.model.<pk>.<relation>` and `app.model.<pk>.<relation>.<action>` -- a row pointing at that one through a foreign key changed, named after the relation (`todoapp.todolist.932.items.created`)

Subscribe as narrowly as the component's content allows.  `app.model` wakes the component for every write to that table in the whole application; `app.model.<pk>.updated` wakes it for the row it shows.

## What fires them, and what does not

The names are fired from Django's `post_save` and `pre_delete`, so anything that bypasses per-instance signals is invisible: `QuerySet.update()`, `bulk_create`, `bulk_update`, raw SQL, and a database-level cascade.  A handler that writes in bulk and needs the page to notice either writes instance by instance, or says so itself with an [event](events.md).

The subscription is recomputed on every render, so it may depend on state: a component showing no row subscribes to nothing, and one that opens a row starts listening to it on the next render.

This is for rows another part of the application writes.  A component that changed the row itself already re-renders -- it needs no subscription to see its own work.
