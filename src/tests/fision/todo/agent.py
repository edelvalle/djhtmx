"""A toy chat agent over the todo list.

Set `AI_PROVIDER_MODEL` to a provider-prefixed model name (`openai:gpt-5.6-luna`,
`anthropic:claude-sonnet-5`) together with the key variable that provider reads
(`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, ...).  Leave it unset and `agent` is `None`,
`is_enabled()` is false, and the chat UI is never mounted.

"""

import os
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from pydantic_ai import Agent, RunContext
from pydantic_ai_harness import SummarizingCompaction

from .models import Item, ItemQS, ItemView

PROVIDER_MODEL = os.environ.get("AI_PROVIDER_MODEL")

INSTRUCTIONS = """
You keep a user's todo list in order.  You can read and search the items, rewrite
their text, and mark them done or not done.

Look an item up before editing it: the editing tools only accept items you have
already listed or searched for.  Your edits are applied when you finish answering,
so a later tool call in the same reply still sees the item's original text.

Prefer acting over asking: if the user says an item is finished, find it and mark it.
Only ask when the request genuinely matches several items and picking the wrong one
would lose work.  When you rewrite an item, keep the author's meaning and voice --
fix grammar and clarity, do not editorialise.  Report what you changed in one short
sentence per item.
"""

# Message count that triggers compaction, and the tail kept verbatim after it.  A
# real deployment would prefer `max_fraction`, which resolves against the model's
# actual context window instead of guessing at a message count.
COMPACT_AFTER_MESSAGES = 1000
KEEP_RECENT_MESSAGES = 100

# Ceiling on the items one tool result may carry, so a long list cannot crowd the
# model's context out on its own.
MAX_ITEMS_PER_RESULT = 50

type EditOutcome = Literal["ok", "unknown item"]


@dataclass
class TodoAgentDeps:
    """Collects the edits one agent run asks for.  Pass it as `deps`, then `flush`."""

    seen_ids: set[UUID] = field(default_factory=set)
    new_texts: dict[UUID, str] = field(default_factory=dict)
    new_completions: dict[UUID, bool] = field(default_factory=dict)

    async def flush(self) -> int:
        """Apply every recorded edit, and return how many items changed.

        An item deleted during the run is skipped, so the count can be lower than the
        number of edits asked for.

        """
        edited = self.new_texts.keys() | self.new_completions.keys()
        changed = 0
        async for item in Item.objects.filter(pk__in=edited):
            item.text = self.new_texts.get(item.pk, item.text)
            item.completed = self.new_completions.get(item.pk, item.completed)
            await item.asave(update_fields=["text", "completed"])
            changed += 1
        return changed


if PROVIDER_MODEL:
    agent = Agent(
        PROVIDER_MODEL,
        # Resolve the model on first use, not at import: a missing provider
        # credential then shows up in the chat bubble rather than stopping the whole
        # todo app from booting.
        defer_model_check=True,
        deps_type=TodoAgentDeps,
        instructions=INSTRUCTIONS,
        capabilities=[
            # Not a hand-rolled history slice: this keeps every tool call together
            # with its return, and a provider rejects an orphaned pair.
            SummarizingCompaction(
                max_messages=COMPACT_AFTER_MESSAGES,
                keep_messages=KEEP_RECENT_MESSAGES,
                # User turns carry the most meaning per token in a chat, so keep the
                # recent ones verbatim next to the summary instead of only the first.
                keep_user_messages=True,
            )
        ],
    )

    @agent.tool
    async def list_items(
        ctx: RunContext[TodoAgentDeps],
        pending_only: bool = False,
    ) -> list[ItemView]:
        """List the todo items, oldest first; a long list comes back truncated.

        Pass `pending_only` to leave out the ones already completed.

        """
        items = Item.objects.active() if pending_only else Item.objects.all()
        return await _viewed(ctx, items)

    @agent.tool
    async def search_items(ctx: RunContext[TodoAgentDeps], text: str) -> list[ItemView]:
        """Find the todo items whose text contains `text`, case-insensitively."""
        return await _viewed(ctx, Item.objects.filter(text__icontains=text))

    @agent.tool
    def set_item_text(ctx: RunContext[TodoAgentDeps], item_id: UUID, text: str) -> EditOutcome:
        """Replace one item's text, to fix grammar or wording.

        `item_id` must come from a previous listing or search -- nothing else
        identifies an item, and an id you have not looked up is refused.  The write
        happens after the run, so reading the item back still shows the old text.

        """
        if item_id in ctx.deps.seen_ids:
            ctx.deps.new_texts[item_id] = text
            return "ok"
        else:
            return "unknown item"

    @agent.tool
    def set_item_completed(
        ctx: RunContext[TodoAgentDeps],
        item_id: UUID,
        completed: bool = True,
    ) -> EditOutcome:
        """Mark one item completed, or pass `completed=False` to reopen it.

        Takes the same `item_id` from a listing or search, and lands at the same time
        as every other edit: after the run.

        """
        if item_id in ctx.deps.seen_ids:
            ctx.deps.new_completions[item_id] = completed
            return "ok"
        else:
            return "unknown item"

else:
    agent = None


def is_enabled() -> bool:
    """Whether the chat feature is configured, and so whether to offer it in the UI."""
    return agent is not None


async def _viewed(ctx: RunContext[TodoAgentDeps], items: ItemQS) -> list[ItemView]:
    """The first `MAX_ITEMS_PER_RESULT` of `items` as views, remembering their ids.

    Every reading tool must go through here: recording the ids is what later lets the
    editing tools accept them.

    """
    found = [item async for item in items[:MAX_ITEMS_PER_RESULT]]
    ctx.deps.seen_ids.update(item.pk for item in found)
    return [item.agent_view() for item in found]
