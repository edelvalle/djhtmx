from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, assert_never
from uuid import UUID

from django.contrib.auth.models import User
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from pydantic import BaseModel, Field

from djhtmx.commands import BuildAndRender, Destroy, Emit, Focus, SkipRender
from djhtmx.component import HtmxComponent, Query
from djhtmx.sse import SSEEventEnvelope, SSESubscription, emit_sse_event
from djhtmx.utils import run_on_commit

from .models import Item


@dataclass
class ItemsCleared:
    pass


class Showing(StrEnum):
    ALL = "all"
    COMPLETED = "completed"
    ACTIVE = "active"


@dataclass(slots=True)
class FilterChanged:
    query: str


class BaseToggleFilter(HtmxComponent, public=False):
    showing: Annotated[Showing, Query("showing"), Field(default=Showing.ALL)]


class BaseQueryFilter(HtmxComponent, public=False):
    query: str = ""

    def _handle_event(self, event: FilterChanged):
        self.query = event.query


@dataclass(slots=True)
class SetEditing:
    item: Item | None


class TodoItemAdded(BaseModel):
    item_id: UUID


class TodoItemUpdated(BaseModel):
    item_id: UUID


class TodoItemRemoved(BaseModel):
    item_id: UUID


class TodoList(BaseToggleFilter, BaseQueryFilter):
    _template_name = "todo/TodoList.html"
    editing: Annotated[Item | None, Query("editing")] = None

    def _handle_event(self, event: SetEditing | FilterChanged):
        if isinstance(event, SetEditing):
            self.editing = event.item
            yield SkipRender(self)
        else:
            super()._handle_event(event)

    @property
    def queryset(self):
        if not self.query:
            return Item.objects.all()
        else:
            return Item.objects.filter(text__icontains=self.query)

    @property
    def items(self):
        match self.showing:
            case Showing.ALL:
                qs = self.queryset
            case Showing.COMPLETED:
                qs = self.queryset.filter(completed=True)
            case Showing.ACTIVE:
                qs = self.queryset.filter(completed=False)
        return qs

    @property
    def editing_items(self):
        return [(item, item == self.editing) for item in self.items]

    @property
    def all_items_are_completed(self):
        return self.items.count() == self.items.completed().count()

    def toggle_all(self, toggle_all: bool = False):
        self.items.update(completed=toggle_all)

    def show(self, showing: Showing):
        self.showing = showing

    def clear_completed(self):
        self.items.completed().delete()

    @property
    def sse_subscriptions(self):
        return {
            SSESubscription(TodoItemAdded, TODO_ITEMS_TOPIC),
            SSESubscription(TodoItemRemoved, TODO_ITEMS_TOPIC),
        }

    def _handle_sse_events(
        self,
        envelope: SSEEventEnvelope[TodoItemAdded | TodoItemRemoved],
    ):
        match envelope.event:
            case TodoItemAdded(item_id=item_id) if envelope.source_session_id != self.session_id:
                # Append the new TodoItem in place; don't re-render the
                # whole TodoList — its full render would replace #todo-list
                # with all items (including the new one), and the appended
                # OOB fragment would arrive after that and add a duplicate.
                yield SkipRender(self)
                if item := self.items.filter(pk=item_id).first():
                    yield BuildAndRender.append(
                        "#todo-list",
                        TodoItem,
                        id=f"item-id-{item.id.hex}",
                        item=item,
                    )
            case TodoItemAdded():
                yield SkipRender(self)
            case TodoItemRemoved():
                # Default render: items-left count and footer visibility
                # depend on the queryset, so re-render the list.  The
                # TodoItem(removed) component self-destroys via its own
                # handler — this just keeps the parent in sync.
                pass


class ListHeader(HtmxComponent):
    _template_name = "todo/ListHeader.html"

    def _handle_event(self, event: ItemsCleared | int):
        pass

    def add(self, new_item: str):
        item = Item.objects.create(text=new_item)
        yield BuildAndRender.append("#todo-list", TodoItem, id=f"item-id-{item.id.hex}", item=item)


class TodoItem(HtmxComponent):
    _template_name = "todo/TodoItem.html"

    item: Item | None
    editing: bool = False

    @property
    def sse_subscriptions(self):
        if self.item:
            topic = todo_item_topic(self.item.id)
            return {
                SSESubscription(TodoItemUpdated, topic),
                SSESubscription(TodoItemRemoved, topic),
            }
        else:
            return set()

    def _handle_sse_events(self, envelope: SSEEventEnvelope[TodoItemUpdated | TodoItemRemoved]):
        match envelope.event:
            case TodoItemUpdated(item_id=item_id) if self.item and item_id == self.item.pk:
                # No yields: framework emits the default Render(self).
                pass
            case TodoItemRemoved(item_id=item_id) if self.item and item_id == self.item.pk:
                yield Destroy(self.id)
            case TodoItemRemoved() | TodoItemUpdated():
                yield SkipRender(self)
            case unreachable:
                assert_never(unreachable)

    def delete(self):
        if self.item:
            self.item.delete()
        yield Destroy(self.id)

    def completed(self, completed: bool = False):
        if self.item:
            self.item.completed = completed
            self.item.save()
        yield SkipRender(self)

    def toggle_editing(self):
        if self.item and not self.item.completed:
            self.editing = not self.editing
        if self.editing:
            yield Focus(f"#{self.id} input[name=text]")
            yield Emit(SetEditing(item=self.item))
        else:
            yield Emit(SetEditing(item=None))

    def save(self, text):
        if self.item:
            self.item.text = text
            self.item.save()
        if self.editing:
            yield from self.toggle_editing()


class TodoCounter(HtmxComponent):
    _template_name = "todo/TodoCounter.html"

    query: Annotated[str, Query("q")] = ""

    def render(self):
        from time import sleep

        sleep(random.random() * 3 + 0.5)

    @property
    def sse_subscriptions(self):
        return {
            SSESubscription(TodoItemAdded, TODO_ITEMS_TOPIC),
            SSESubscription(TodoItemUpdated, TODO_ITEMS_TOPIC),
            SSESubscription(TodoItemRemoved, TODO_ITEMS_TOPIC),
        }

    def _handle_sse_events(
        self,
        envelope: SSEEventEnvelope[TodoItemAdded | TodoItemUpdated | TodoItemRemoved],
    ):
        # No yields: framework emits the default Render(self).
        return

    @property
    def items(self):
        return Item.objects.active()


class TodoFilter(HtmxComponent):
    _template_name = "todo/TodoFilter.html"
    query: Annotated[str, Query("q")] = ""

    def set_query(self, query: str = ""):
        self.query = query.strip()
        yield Emit(FilterChanged(self.query))


class LoggedUserCounter(HtmxComponent):
    """A component that cannot exist without a logged-in user.

    The non-optional `user` annotation is the whole declaration: djhtmx refuses to build it for an
    anonymous request and sends the visitor to the login page instead.

    """

    _template_name = "todo/LoggedUserCounter.html"

    user: Annotated[User, Field(exclude=True)]
    counter: int = 0

    def inc(self, amount: int = 1):
        self.counter += amount


def todo_item_topic(item_id: UUID):
    return f"{TODO_ITEMS_TOPIC}.{item_id}"


TODO_ITEMS_TOPIC = "todo.item"


@receiver(post_save, sender=Item)
def emit_todo_item_updated(sender, instance: Item, created: bool, **kwargs):
    item_id = instance.pk
    event = TodoItemAdded(item_id=item_id) if created else TodoItemUpdated(item_id=item_id)
    run_on_commit(
        emit_sse_event,
        event,
        topics={TODO_ITEMS_TOPIC, todo_item_topic(item_id)},
    )


@receiver(post_delete, sender=Item)
def emit_todo_item_removed(sender, instance: Item, **kwargs):
    item_id = instance.pk
    run_on_commit(
        emit_sse_event,
        TodoItemRemoved(item_id=item_id),
        topics={TODO_ITEMS_TOPIC, todo_item_topic(item_id)},
    )
