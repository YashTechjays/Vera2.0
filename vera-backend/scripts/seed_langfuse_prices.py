"""Seed the Langfuse model price entries Vera's usage attributes are priced against.

Vera's spans carry raw usage only (`langfuse.observation.usage_details`); Langfuse does
the arithmetic. It can only do so if a model definition exists whose per-usage-type
price keys match the usage keys we send — otherwise usage ingests fine and every
observation renders BLANK cost, which looks exactly like broken instrumentation.

Rates are read from the environment, never hardcoded and deliberately NOT in Settings:
the application never needs a price, so this keeps exactly one place prices live and no
second copy inside Vera to drift.

    just langfuse-seed-prices

    LANGFUSE_PRICE_STT_FLUX_PER_MS              Deepgram Flux, $ per MILLISECOND of audio
    LANGFUSE_PRICE_STT_NOVA_PER_MS              Deepgram Nova, $ per MILLISECOND of audio
    LANGFUSE_PRICE_TTS_SONIC_PER_CHARACTER      Cartesia Sonic, $ per character
    LANGFUSE_PRICE_LLM_GEMINI_INPUT_PER_TOKEN   Gemini, $ per uncached input token
    LANGFUSE_PRICE_LLM_GEMINI_OUTPUT_PER_TOKEN  Gemini, $ per output token
    LANGFUSE_PRICE_LLM_GEMINI_CACHED_PER_TOKEN  Gemini, $ per CACHED input token

The audio rates are PER MILLISECOND because Langfuse stores usage as integers and
fractional seconds are truncated. Published rates are per minute, so converting is
`per_minute / 60000` — entering the per-minute figure directly overstates cost by
60,000x while still rendering a plausible dollar amount.

Public list prices (~$0.0077/min Deepgram Nova streaming, ~$0.0065/min Flux, $5-37 per
million Cartesia characters) are a SANITY REFERENCE ONLY. Use your contracted rates.

Idempotent: `POST /api/public/models` upserts only when handed an existing `modelId`,
and rejects a duplicate `modelName` without one, so this GETs the model list first and
threads any existing id back in. Re-running is a no-op-shaped update.

Writes to whatever VERA_LANGFUSE_HOST resolves to — the target host is logged.
"""

import asyncio
import base64
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from vera_core.config import get_settings
from vera_core.observability.usage_spans import (
    STT_AUDIO_MS,
    TTS_CHARACTERS,
    USAGE_CACHED,
    USAGE_INPUT,
    USAGE_OUTPUT,
)

logger = logging.getLogger("vera.seed_langfuse_prices")


class MissingRateError(RuntimeError):
    """A rate env var is absent or unparseable. Refuse to seed rather than write a
    $0.00 entry, which is indistinguishable from broken instrumentation in the UI."""


@dataclass(frozen=True)
class ModelPrice:
    model_name: str
    # A FAMILY regex, not an exact version: bumping sonic-3.5 -> sonic-4 must not
    # silently zero the cost, and a missing match renders identically to "no data".
    match_pattern: str
    # usage key -> env var holding its rate. Keys MUST equal what the instrumentation
    # puts in usage_details (vera_core.observability.usage_spans / llm_usage_export).
    env_vars: Mapping[str, str] = field(default_factory=dict)


MODELS: tuple[ModelPrice, ...] = (
    ModelPrice(
        "vera-deepgram-flux",
        r"(?i)^flux-.*$",
        {STT_AUDIO_MS: "LANGFUSE_PRICE_STT_FLUX_PER_MS"},
    ),
    ModelPrice(
        "vera-deepgram-nova",
        r"(?i)^nova-.*$",
        {STT_AUDIO_MS: "LANGFUSE_PRICE_STT_NOVA_PER_MS"},
    ),
    ModelPrice(
        "vera-cartesia-sonic",
        r"(?i)^sonic-.*$",
        {TTS_CHARACTERS: "LANGFUSE_PRICE_TTS_SONIC_PER_CHARACTER"},
    ),
    # One family entry covers every Gemini surface (cascade, observer, health, summary,
    # post-call eval). The `cached` key is REQUIRED: without it Langfuse prices cache
    # hits at $0 and understates cost — the mirror image of the bug the export-time
    # correction fixes.
    ModelPrice(
        "vera-gemini",
        r"(?i)^gemini-.*$",
        {
            USAGE_INPUT: "LANGFUSE_PRICE_LLM_GEMINI_INPUT_PER_TOKEN",
            USAGE_OUTPUT: "LANGFUSE_PRICE_LLM_GEMINI_OUTPUT_PER_TOKEN",
            USAGE_CACHED: "LANGFUSE_PRICE_LLM_GEMINI_CACHED_PER_TOKEN",
        },
    ),
)


def resolve_rates(env: Mapping[str, str]) -> dict[str, float]:
    """Every rate every model needs, or MissingRateError. All-or-nothing on purpose:
    a partially-priced project renders some observations at $0, which reads as
    "this surface is free" rather than "this seed was incomplete"."""
    rates: dict[str, float] = {}
    missing: list[str] = []
    for model in MODELS:
        for env_var in model.env_vars.values():
            raw = env.get(env_var)
            if raw is None:
                missing.append(env_var)
                continue
            try:
                rates[env_var] = float(raw)
            except ValueError:
                missing.append(env_var)
    if missing:
        raise MissingRateError(f"missing or unparseable rate env vars: {sorted(set(missing))}")
    return rates


def build_payload(
    price: ModelPrice, model_id: str | None, *, rates: Mapping[str, float]
) -> dict[str, Any]:
    """The POST body for one model entry. Prices go in pricingTiers[0].prices — the
    deprecated flat inputPrice/outputPrice cannot express a custom usage key at all."""
    payload: dict[str, Any] = {
        "modelName": price.model_name,
        "matchPattern": price.match_pattern,
        "pricingTiers": [
            {"prices": {key: rates[env_var] for key, env_var in price.env_vars.items()}}
        ],
    }
    if model_id is not None:
        payload["modelId"] = model_id
    return payload


async def _existing_ids(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.get("/api/public/models", params={"limit": 100})
    response.raise_for_status()
    data: list[dict[str, Any]] = response.json().get("data", [])
    return {m["modelName"]: m["id"] for m in data if "modelName" in m and "id" in m}


async def seed(client: httpx.AsyncClient, rates: Mapping[str, float]) -> list[str]:
    """Upsert every entry; returns the model names written."""
    existing = await _existing_ids(client)
    written: list[str] = []
    for price in MODELS:
        payload = build_payload(price, existing.get(price.model_name), rates=rates)
        response = await client.post("/api/public/models", json=payload)
        response.raise_for_status()
        written.append(price.model_name)
        logger.info(
            "seeded %s (matchPattern=%s, usage keys=%s)",
            price.model_name,
            price.match_pattern,
            sorted(price.env_vars),
        )
    return written


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = get_settings()
    if not settings.langfuse_host:
        logger.error("VERA_LANGFUSE_HOST is not set — nothing to seed")
        return 1
    try:
        rates = resolve_rates(os.environ)
    except MissingRateError as exc:
        logger.error("%s", exc)
        logger.error("refusing to seed: a $0.00 price looks identical to broken tracing")
        return 1

    token = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
    ).decode()
    logger.info("seeding model prices into %s", settings.langfuse_host)
    async with httpx.AsyncClient(
        base_url=settings.langfuse_host.rstrip("/"),
        headers={"Authorization": f"Basic {token}"},
        timeout=30.0,
    ) as client:
        written = await seed(client, rates)
    logger.info("seeded %d model price entries: %s", len(written), ", ".join(written))
    # Configured selectors that match no entry above would render blank cost silently.
    logger.info(
        "verify these configured models match a pattern above: %s",
        sorted(
            {
                settings.voice_llm_default_model,
                settings.gemini_flash_model,
                settings.summary_primary_model,
                settings.observer_extract_primary_model,
                settings.health_primary_model,
                settings.whisper_stt_primary_model,
                *settings.summary_fallback_models,
                *settings.observer_extract_fallback_models,
                *settings.health_fallback_models,
                *settings.whisper_stt_fallback_models,
            }
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
