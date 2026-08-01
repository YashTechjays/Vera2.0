"""One place where a tool call's model-authored `reason` reaches the worker log.

Every LLM tool takes a required `reason` so a call can explain itself. That text is written by
Gemini about live call state, so it can carry a member name or a clinical detail — unloggable in
production (vera_core/CLAUDE.md: raw transcript-derived text is scrubbed to IDs, counts and
shapes before emit). So the verbatim reason is gated behind `VERA_LOG_TOOL_REASONS`, off by
default, and the ungated path logs only the shape — matching `takeover_transcript.py`'s
`len(text)` and `ivr_agent.press_keypad`'s digit count.

Its own module because no existing module is importable by all three tool-carrying files:
`agent.py` imports `ivr_agent.py`, and `plan_runtime.py` imports `agent.py`.
"""

import logging

from vera_core.config.settings import get_settings

logger = logging.getLogger("agent_worker")


def log_tool_reason(tool: str, reason: str) -> None:
    """Log why the model called `tool` — verbatim only when the env flag allows it."""
    # Read at call time: get_settings is lru_cached, so a module-level read would freeze the
    # flag at import, before a deployment env or a test could set it.
    if get_settings().log_tool_reasons:
        logger.info("tool %s: %s", tool, reason)
    else:
        logger.info("tool %s: reason len=%d", tool, len(reason))
