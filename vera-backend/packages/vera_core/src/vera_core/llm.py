"""Fault-tolerant LLM invocation for out-of-pipeline calls (summaries, analytics,
extraction) — NOT the live voice cascade (that stays in the agent worker's
AgentSession config).

Wraps livekit-agents' FallbackAdapter: callers declare an ordered chain of
provider/model selectors and get plain strings back; on a provider error or
attempt timeout the adapter moves to the next model transparently. LiveKit types
never cross this module's boundary — every caller in the codebase MUST go through
ResilientLLM rather than instantiating provider SDK / plugin clients directly
(see this package's CLAUDE.md).

PHI: prompts and completions routinely carry PHI. Nothing in this module logs
prompt/response text or exception reprs — provider errors can embed request
payloads — only exception type names and provider/model labels.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vera_core.config.secrets import SecretProvider

if TYPE_CHECKING:
    from livekit.agents.llm import LLM

logger = logging.getLogger(__name__)

OPENAI_API_KEY_SECRET = "OPENAI_API_KEY"

type ProviderFactory = Callable[["LLMSpec", SecretProvider | None], "LLM[Any]"]


class LLMUnavailableError(Exception):
    """Every provider in the chain failed. Carries no prompt/response text."""


@dataclass(frozen=True)
class LLMSpec:
    """One provider/model selector, e.g. LLMSpec("google", "gemini-3.1-flash-lite")."""

    provider: str
    model: str
    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, selector: str) -> LLMSpec:
        """Parse a "provider:model" selector (the settings/env representation)."""
        provider, sep, model = selector.partition(":")
        if not sep or not provider.strip() or not model.strip():
            raise ValueError(f"invalid LLM selector {selector!r}; expected 'provider:model'")
        return cls(provider=provider.strip(), model=model.strip())


@dataclass(frozen=True)
class FallbackOptions:
    """Per-attempt budget and retry pacing, passed through to FallbackAdapter."""

    attempt_timeout: float = 8.0
    max_retry_per_llm: int = 1
    retry_interval: float = 0.5


def _build_google(spec: LLMSpec, secrets: SecretProvider | None) -> LLM[Any]:
    # Vertex AI path (ADC / Workload Identity creds) — same in-boundary route as
    # the live pipeline's cascade LLM.
    from livekit.plugins import google

    return google.LLM(model=spec.model, vertexai=True, **spec.extra)


def _build_openai(spec: LLMSpec, secrets: SecretProvider | None) -> LLM[Any]:
    # OpenAI API — in-boundary under the signed BAA (repo-root CLAUDE.md trust
    # boundary). Key comes from the SecretProvider, never read from env directly.
    from livekit.plugins import openai

    if secrets is None:
        raise ValueError("openai provider requires a SecretProvider for OPENAI_API_KEY")
    api_key = secrets.get(OPENAI_API_KEY_SECRET)
    return openai.LLM(model=spec.model, api_key=api_key, **spec.extra)


PROVIDERS: Mapping[str, ProviderFactory] = {
    "google": _build_google,
    "openai": _build_openai,
}


class ResilientLLM:
    """Fault-tolerant completion client over an ordered provider chain.

    Providers are validated at construction; the underlying plugin clients and
    the FallbackAdapter are built lazily on first complete() — LiveKit LLM
    clients open aiohttp sessions that need a running event loop (same rule as
    LiveKitGateway). Call aclose() at shutdown.
    """

    def __init__(
        self,
        primary: LLMSpec,
        fallbacks: Sequence[LLMSpec] = (),
        *,
        options: FallbackOptions = FallbackOptions(),  # noqa: B008 — frozen, immutable
        secrets: SecretProvider | None = None,
        registry: Mapping[str, ProviderFactory] | None = None,
    ) -> None:
        self._specs: list[LLMSpec] = [primary, *fallbacks]
        self._options = options
        self._secrets = secrets
        self._registry = PROVIDERS if registry is None else registry
        for spec in self._specs:
            if spec.provider not in self._registry:
                raise ValueError(f"unknown LLM provider {spec.provider!r}")
        self._llms: list[LLM[Any]] = []
        self._chain: Any = None

    def _adapter(self) -> Any:
        if self._chain is None:
            from livekit.agents.llm import FallbackAdapter

            self._llms = [self._registry[s.provider](s, self._secrets) for s in self._specs]
            self._chain = FallbackAdapter(
                self._llms,
                attempt_timeout=self._options.attempt_timeout,
                max_retry_per_llm=self._options.max_retry_per_llm,
                retry_interval=self._options.retry_interval,
            )
        return self._chain

    async def complete(self, *, system: str, user: str) -> str:
        """One-shot completion: system + user message in, completion text out.

        Raises LLMUnavailableError when the whole chain is exhausted.
        """
        from livekit.agents.llm import ChatContext

        chat_ctx = ChatContext.empty()
        chat_ctx.add_message(role="system", content=system)
        chat_ctx.add_message(role="user", content=user)
        try:
            response = await self._adapter().chat(chat_ctx=chat_ctx).collect()
        except Exception as exc:  # payloads may carry PHI — type name only
            logger.warning("all LLM providers failed: %s", type(exc).__name__)
            raise LLMUnavailableError from exc
        return str(response.text)

    async def aclose(self) -> None:
        chain, llms = self._chain, self._llms
        self._chain, self._llms = None, []
        if chain is not None:
            await chain.aclose()
        for llm in llms:
            await llm.aclose()
