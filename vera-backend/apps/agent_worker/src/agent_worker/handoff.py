"""Cross-cutting handoff utilities, depending only on the LiveKit ``Agent`` base.

Kept in its own leaf module (imports neither ``agent`` nor ``ivr_agent``) so every
handoff site — the plan runtime, the IVR navigator, and the persona agents — can
import it top-level without the ``agent`` <-> ``ivr_agent`` import cycle.

Both carry functions return the target's resulting item ids: the caller stores that and
passes it back as ``inherited_ids`` at the target's own later handoff, which is how
``own_items`` tells an agent's own turns from the ones it was handed. Ids survive
``ChatContext.merge`` (it dedupes on ``item.id``), so a boundary stays valid all call.
"""

from collections.abc import Iterable

from livekit.agents import Agent
from livekit.agents.llm import ChatContext, ChatItem


def _item_ids(agent: Agent) -> frozenset[str]:
    return frozenset(item.id for item in agent.chat_ctx.items)


def own_items(source: Agent, inherited_ids: frozenset[str]) -> list[ChatItem]:
    """The turns `source` produced itself, rather than inherited at its own handoff."""
    return [item for item in source.chat_ctx.items if item.id not in inherited_ids]


async def carry_chat_ctx(source: Agent, target: Agent) -> frozenset[str]:
    """Copy the source agent's whole spoken conversation into `target`.

    A tool-returned agent keeps whatever chat_ctx it was constructed with — empty for our
    pre-built plan/IVR agents — and LiveKit only auto-carries history for inline AgentTasks,
    NOT for this handoff shape. Without this, every task and the IVR→plan handoff drops the
    call so far: the next agent re-greets and re-asks. Cumulative, since `source` already
    holds every task before it; `carry_items` is the bounded alternative. No-op-safe: called
    before the target is active."""
    carried = target.chat_ctx.copy().merge(
        source.chat_ctx, exclude_instructions=True, exclude_function_call=True
    )
    await target.update_chat_ctx(carried)
    return _item_ids(target)


async def carry_items(target: Agent, items: Iterable[ChatItem]) -> frozenset[str]:
    """Replace `target`'s conversation with `items`, deduped on id, order preserved.

    Order is the caller's: the carry set spans several agents, so there is no single
    chronological sort `merge` could apply."""
    seen: set[str] = set()
    ordered: list[ChatItem] = []
    for item in items:
        if item.id not in seen:
            seen.add(item.id)
            ordered.append(item)
    # copy() applies the same two filters as merge(): the source's own system prompt (the
    # target keeps its own) and tool-call bookkeeping never cross a handoff.
    carried = ChatContext(ordered).copy(exclude_instructions=True, exclude_function_call=True)
    await target.update_chat_ctx(carried)
    return _item_ids(target)
