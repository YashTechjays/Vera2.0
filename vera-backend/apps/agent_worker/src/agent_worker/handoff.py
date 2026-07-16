"""Cross-cutting handoff utilities, depending only on the LiveKit ``Agent`` base.

Kept in its own leaf module (imports neither ``agent`` nor ``ivr_agent``) so every
handoff site — the plan runtime, the IVR navigator, and the persona agents — can
import it top-level without the ``agent`` <-> ``ivr_agent`` import cycle.
"""

from livekit.agents import Agent


async def carry_chat_ctx(source: Agent, target: Agent) -> None:
    """Copy the source agent's spoken conversation into `target` before a handoff.

    A tool-returned agent keeps whatever chat_ctx it was constructed with — empty
    for our pre-built plan/IVR agents — and LiveKit only auto-carries history for
    inline AgentTasks, NOT for this handoff shape. Without this, every task and
    the IVR→plan handoff drops the call so far: the next agent re-greets and
    re-asks already-answered questions. Merge the source's items into the target,
    excluding the source's own instructions (the target keeps its own) and
    internal function-call bookkeeping — the same filters LiveKit's own resume
    merge uses (voice/agent.py). No-op-safe: called before the target is active."""
    carried = target.chat_ctx.copy().merge(
        source.chat_ctx, exclude_instructions=True, exclude_function_call=True
    )
    await target.update_chat_ctx(carried)
