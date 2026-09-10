from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, assert_never
from uuid import UUID

from django.contrib.auth.models import User
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from pydantic import BaseModel, Field
from pydantic_ai import (
    AgentRunResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)
from pydantic_ai.messages import ModelMessagesTypeAdapter

from djhtmx.commands import BuildAndRender, Destroy, Emit, Focus, SkipRender
from djhtmx.component import HtmxComponent, Query
from djhtmx.sse import SSEEventEnvelope, SSESubscription, aemit_sse_event, emit_sse_event
from djhtmx.utils import run_on_commit

from .agent import TodoAgentDeps, agent
from .models import ChatMessage, Conversation, Item, Role, Status


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


class AgentReplyUpdated(BaseModel):
    message_id: UUID


class AgentChat(HtmxComponent):
    """A chat panel over the todo list.  Mount it only when `todo.agent.is_enabled()`."""

    _template_name = "todo/AgentChat.html"

    conversation: Conversation | None = None

    @property
    def messages(self):
        if self.conversation:
            return self.conversation.messages.all()
        else:
            return ChatMessage.objects.none()

    async def send(self, prompt: str):
        """Record the prompt and open an empty reply for the agent to fill."""
        # An async generator, not a plain `async def`: `validate_call` wraps a
        # coroutine handler that takes arguments into something the dispatcher no
        # longer recognises as async, and the body then never runs.
        prompt = prompt.strip()
        # Append rather than let the panel re-render: a full render replaces the
        # scroll container, which resets it to the top of the conversation.
        yield SkipRender(self)
        if prompt:
            conversation = self.conversation or await Conversation.objects.acreate()
            self.conversation = conversation
            position = await conversation.messages.acount()
            question = await ChatMessage.objects.acreate(
                conversation=conversation,
                position=position,
                role=Role.USER,
                text=prompt,
            )
            # The agent runs in a second dispatch: this one has to return for the
            # bubble to reach the DOM before any streamed delta can land on it.
            reply = await ChatMessage.objects.acreate(
                conversation=conversation,
                position=position + 1,
                role=Role.AGENT,
                status=Status.PENDING,
            )
            for message in (question, reply):
                yield BuildAndRender.append(
                    "#agent-chat-log",
                    AgentChatMessage,
                    parent_id=self.id,
                    id=message.component_id,
                    message=message,
                )
        yield Focus(f"#{self.id} input[name=prompt]")


class AgentChatMessage(HtmxComponent):
    """One chat bubble.  An agent reply also owns the agent run that fills it."""

    _template_name = "todo/AgentChatMessage.html"

    message: ChatMessage | None

    @property
    def sse_subscriptions(self):
        if self.message:
            return {SSESubscription(AgentReplyUpdated, agent_message_topic(self.message.id))}
        else:
            return set()

    def _handle_sse_events(self, envelope: SSEEventEnvelope[AgentReplyUpdated]):
        # No yields: the default render re-reads `message` from the database, which is
        # the whole mechanism -- the reply grows in place as `stream` writes it.
        return

    async def stream(self):
        """Run the agent for this reply, publishing its text as it arrives.

        Triggered once per reply, by the bubble itself; a reply that is no longer
        `PENDING` is left alone.

        """
        # Occupies one sync-work pool thread, and so one DB connection, for as long
        # as the model takes: concurrent chats are bounded by `DJHTMX_SYNC_WORKERS`,
        # shared with every other dispatch.
        yield SkipRender(self)

        message = self.message
        if agent and message and message.status == Status.PENDING:
            conversation = await Conversation.objects.aget(pk=message.conversation_id)
            prompt = await conversation.messages.aget(position=message.position - 1)
            history = ModelMessagesTypeAdapter.validate_python(conversation.history)

            # Leave PENDING before calling the model: the re-renders this handler
            # causes would otherwise ask for a second run of the same reply.
            message.status = Status.STREAMING
            await message.asave(update_fields=["status"])

            text = ""
            flushed = 0
            deps = TodoAgentDeps()
            try:
                async with agent.run_stream_events(
                    prompt.text,
                    message_history=history,
                    conversation_id=str(conversation.pk),
                    deps=deps,
                ) as events:
                    async for event in events:
                        match event:
                            # The first chunk of a text part arrives as the part
                            # itself; only the ones after it are deltas.  Matching
                            # deltas alone silently drops the opening words.
                            case (
                                PartStartEvent(part=TextPart(content=str() as delta))
                                | PartDeltaEvent(delta=TextPartDelta(content_delta=delta))
                            ):
                                text += delta
                                # Publishing over SSE, not yielding: commands yielded
                                # here are drained into a list and delivered only
                                # once the handler returns.
                                if len(text) - flushed >= FLUSH_EVERY_CHARS:
                                    flushed = len(text)
                                    await self._publish(message, text)
                            case AgentRunResultEvent(result=result):
                                # `all_messages()` rather than appending
                                # `new_messages()` (the cheaper write the docs
                                # suggest for a plain chat): compaction rewrites the
                                # history in place, so only storing the whole thing
                                # carries the summary forward -- appending would keep
                                # replaying the turns it just collapsed.
                                conversation.history = ModelMessagesTypeAdapter.dump_python(
                                    result.all_messages(), mode="json"
                                )
                                await conversation.asave(update_fields=["history"])
            except Exception as error:
                # Own the failure instead of letting the dispatcher's handler swallow
                # it: a reply left `PENDING` would ask to be run again on every
                # reload.
                logger.exception("The todo agent failed to answer %s", message.pk)
                await self._publish(message, f"{text}\n\n⚠ {error}", status=Status.FAILED)
            else:
                # The tools only recorded their edits; this is where they land, out
                # of the run and back under our own DB connection.
                await deps.flush()
                await self._publish(message, text, status=Status.DONE)

    async def _publish(self, message: ChatMessage, text: str, status: str | None = None):
        """Persist the reply's text so far and wake every browser showing it."""
        # Stripped: models like to open a reply with a newline, and `pre-wrap`
        # would render it as a blank first line.
        message.text = text.strip()
        updated = ["text"]
        if status:
            message.status = status
            updated.append("status")
        await message.asave(update_fields=updated)
        await aemit_sse_event(
            AgentReplyUpdated(message_id=message.pk),
            topics=[agent_message_topic(message.pk)],
        )


def agent_message_topic(message_id: UUID):
    return f"todo.agent.message.{message_id}"


# Characters of reply text between database writes and SSE publishes.  Low enough to
# read as streaming, high enough that a long answer costs tens of round trips rather
# than one per token.
FLUSH_EVERY_CHARS = 24

logger = logging.getLogger(__name__)
