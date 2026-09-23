# SSE events

An [event](events.md) reaches the components of one page, inside the request that emitted it.  An SSE event crosses processes: something happening in a worker, a management command or another person's request reaches every page that subscribed to it.  Use it when a change one person makes has to show up on somebody else's screen without them acting.

The page needs the `SSEEventRouter` component once, normally in the base template.  It opens the single SSE connection and routes what arrives into the DOM:

```html
{% htmx "SSEEventRouter" %}
```

## Subscribing

A component subscribes by declaring **both** members.  With only one of them djhtmx logs a warning and the component simply never receives anything -- nothing raises, so this is worth checking first when events do not arrive:

```python
class NotificationCenter(HtmxComponent):
    _template_name = "NotificationCenter.html"

    @property
    def sse_subscriptions(self) -> set[SSESubscription]:
        if user := self.user:
            return {SSESubscription(NotificationDispatched, topic=f"notifications:{user.id}")}
        else:
            return set()

    def _handle_sse_events(self, envelope: SSEEventEnvelope[NotificationDispatched]):
        match envelope.event:
            case NotificationDispatched(body=body):
                self.messages = [*self.messages, body]
            case unreachable:
                assert_never(unreachable)
```

The handler's annotation is what declares the event types the component accepts, exactly as `_handle_event` does.  A `SSESubscription` naming a type the annotation does not accept is dropped -- again with a warning and nothing else.

`sse_subscriptions` is recomputed on every render, so it may depend on state: a component with no user subscribes to nothing, and one that opens a row starts listening for that row on the next render.

## Topics

A topic is a string your application invents, and it is how an event reaches the right components instead of all of them.  Put an identifier in it -- `notifications:{user_id}`, `todo.item.{item_id}` -- so a single event wakes the few components that care.  A topic like `"notifications"` wakes every open page in the installation.

## Emitting

```python
from djhtmx.sse import emit_sse_event
from djhtmx.utils import run_on_commit


class NotificationDispatched(BaseModel):
    notification_id: UUID
    body: str


@receiver(post_save, sender=Notification)
def announce_notification(sender, instance: Notification, created: bool, **kwargs):
    if created:
        run_on_commit(
            emit_sse_event,
            NotificationDispatched(notification_id=instance.pk, body=instance.body),
            topics={f"notifications:{instance.user_id}"},
        )
```

Three things this shape is protecting against:

- **The event must be a pydantic `BaseModel`.**  It is serialized to Redis, which a `dataclass` will not survive -- and that is the difference from an in-page event, which is a dataclass.
- **`run_on_commit`, always.**  The consumer is another process reading the database on its own: an event published before the transaction commits arrives at a worker that cannot see the row yet.  (It runs immediately under tests, so tests still observe it.)
- **An event nothing listens to is dropped silently.**  `emit_sse_event` returns without doing anything when no component's `_handle_sse_events` annotates that type.  A component removed, renamed, or never imported turns emission into a no-op with no error anywhere.

## The session that emitted gets it too

The event reaches every subscribed page, including the one whose handler caused it.  When that handler already drew the change, the arriving event would draw it a second time.  `envelope.source_session_id` is how the handler tells its own work apart:

```python
def _handle_sse_events(self, envelope: SSEEventEnvelope[TodoItemAdded]):
    match envelope.event:
        case TodoItemAdded(item_id=item_id) if envelope.source_session_id != self.session_id:
            yield SkipRender(self)
            if item := self.items.filter(pk=item_id).first():
                yield BuildAndRender.append("#todo-list", TodoItem, id=f"item-{item_id}", item=item)
        case TodoItemAdded():
            yield SkipRender(self)
```

## The handler is a handler

`_handle_sse_events` mutates state and yields commands like any other, and it gets the same default render -- so yield `SkipRender(self)` when the event changed nothing this component shows.  A failure inside it is trapped and reported the way [errors](errors.md) describes.

One thing differs: the default render of an SSE wake-up respects the component's own laziness, so a component placed with `lazy=True` falls back to its placeholder every time an event arrives.  See [lazy-components](lazy-components.md).
