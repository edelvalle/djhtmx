from typing import TypedDict
from uuid import uuid4

from django.db import models


class ItemView(TypedDict):
    """One `Item` as the todo agent's tools return it.

    `id` is the UUID in string form and is what the editing tools take back;
    `deadline` is ISO-8601, or `None` when unset.

    """

    id: str
    text: str
    completed: bool
    deadline: str | None


class BaseModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)

    class Meta:
        abstract = True


class ItemQS(models.QuerySet["Item"]):
    def completed(self):
        return self.filter(completed=True)

    def active(self):
        return self.filter(completed=False)


class Item(BaseModel):
    completed = models.BooleanField(default=False)
    text = models.CharField(max_length=256)
    description = models.TextField(null=True, default=None)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    deadline = models.DateTimeField(null=True, default=None, db_index=True)

    objects: ItemQS = ItemQS.as_manager()  # type: ignore

    class Meta:
        ordering = ["timestamp"]

    def __str__(self):
        return self.text

    def agent_view(self) -> ItemView:
        """This item in the shape the todo agent's tools return."""
        return {
            "id": str(self.id),
            "text": self.text,
            "completed": self.completed,
            "deadline": self.deadline.isoformat() if self.deadline else None,
        }


class Conversation(BaseModel):
    """A chat session with the todo agent.

    `history` is what the agent continues from: a pydantic-ai message list, read and
    written with `ModelMessagesTypeAdapter`.  It is compacted, so older turns are
    summaries rather than the real exchange -- read `messages` for the transcript.

    """

    history = models.JSONField(default=list)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    # `ChatMessage.conversation`'s `related_name`.  Spelled out because django-types
    # runs plugin-free and cannot infer a reverse accessor from the other end.
    messages: "models.Manager[ChatMessage]"

    class Meta:
        ordering = ["timestamp"]


class Role(models.TextChoices):
    USER = "user"
    AGENT = "agent"


class Status(models.TextChoices):
    """Lifecycle of an agent reply: awaiting its run, receiving text, then settled.

    Only a `PENDING` reply is ever run, and only once.

    """

    PENDING = "pending"
    STREAMING = "streaming"
    DONE = "done"
    FAILED = "failed"


class ChatMessage(BaseModel):
    """One turn in a `Conversation`, as shown to the user.

    `position` both orders the turns and pairs them: an agent reply at position `n`
    answers the user message at `n - 1`.  A user message is written once and is
    always `DONE`; an agent reply's `text` grows as the run streams it.

    """

    conversation = models.ForeignKey(
        Conversation, related_name="messages", on_delete=models.CASCADE
    )
    position = models.PositiveIntegerField()
    role = models.CharField(max_length=8, choices=Role)
    text = models.TextField(default="")
    status = models.CharField(max_length=16, choices=Status, default=Status.DONE)

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "position"], name="unique_turn_position"
            )
        ]

    def __str__(self):
        return f"{self.role}: {self.text[:40]}"

    @property
    def component_id(self) -> str:
        """The id of the component that draws this turn.

        Carries `position` because djhtmx orders sibling renders by component id, so
        an id that sorts chronologically is what keeps several bubbles appended in
        one dispatch in the order they were written.

        """
        return f"chat-msg-{self.position:06d}-{self.id.hex}"
