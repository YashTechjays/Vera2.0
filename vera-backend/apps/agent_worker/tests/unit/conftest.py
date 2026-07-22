"""Shared helpers for the agent_worker unit tests."""

from livekit.agents import Agent


def chat_ctx_texts(agent: Agent) -> list[str]:
    """The plain-string message contents of an agent's chat_ctx, in order — the
    turns a handoff must carry forward (used to assert history is preserved)."""
    return [
        content
        for item in agent.chat_ctx.items
        if item.type == "message"
        for content in item.content
        if isinstance(content, str)
    ]
