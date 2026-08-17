"""Seed the Langfuse model price entries Vera's usage attributes are priced against.

Vera's spans carry raw usage only (`langfuse.observation.usage_details`); Langfuse does
the arithmetic. It can only do so if a model definition exists whose per-usage-type
price keys match the usage keys we send — otherwise usage ingests fine and every
observation renders BLANK cost, which looks exactly like broken instrumentation.

Rates are read from the environment, never hardcoded and deliberately NOT in Settings:
the application never needs a price, so this keeps exactly one place prices live and no
second copy inside Vera to drift.

    just langfuse-seed-prices

    LANGFUSE_PRICE_STT_FLUX_PER_MS          Deepgram Flux, $ per MILLISECOND of audio
    LANGFUSE_PRICE_STT_NOVA_PER_MS          Deepgram Nova, $ per MILLISECOND of audio
    LANGFUSE_PRICE_TTS_SONIC_PER_CHARACTER  Cartesia Sonic, $ per character

Each Gemini model in GEMINI_MODELS is priced SEPARATELY — they differ by ~2x — so
each needs three vars named after it, e.g. for gemini-3.6-flash:

    LANGFUSE_PRICE_LLM_GEMINI_3_6_FLASH_INPUT_PER_TOKEN    $ per uncached input token
    LANGFUSE_PRICE_LLM_GEMINI_3_6_FLASH_OUTPUT_PER_TOKEN   $ per output token
    LANGFUSE_PRICE_LLM_GEMINI_3_6_FLASH_CACHED_PER_TOKEN   $ per CACHED input token

Run it with none set to have it list every name it wants.

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
import math
import os
import re
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

# Runaway guard for the model-listing walk, not an expected bound: ~160 built-ins
# plus our handful fit well inside this.
_MAX_MODEL_PAGES = 20


class MissingRateError(RuntimeError):
    """A rate env var is absent, unparseable, or not a strictly positive finite number.
    Refuse to seed rather than write a $0.00 entry, which is indistinguishable from
    broken instrumentation in the UI."""


# Langfuse's own unit enum, validated server-side. MILLISECONDS being first-class
# is what makes the per-millisecond audio decision the native one rather than a
# workaround: usage stays integral AND the unit is declared, so the UI reads right.
UNIT_MILLISECONDS = "MILLISECONDS"
UNIT_CHARACTERS = "CHARACTERS"
UNIT_TOKENS = "TOKENS"

# Every pricing tier must be named, ordered, and carry its (possibly empty) match
# conditions, and EXACTLY ONE tier per model must be flagged isDefault. Vera prices
# flat, so each model gets one unconditional default tier.
_DEFAULT_TIER_NAME = "default"


@dataclass(frozen=True)
class ModelPrice:
    model_name: str
    # A FAMILY regex, not an exact version: bumping sonic-3.5 -> sonic-4 must not
    # silently zero the cost, and a missing match renders identically to "no data".
    match_pattern: str
    # usage key -> env var holding its rate. Keys MUST equal what the instrumentation
    # puts in usage_details (vera_core.observability.usage_spans / llm_usage_export).
    env_vars: Mapping[str, str] = field(default_factory=dict)
    # Langfuse validates this against its own enum; it must match what the usage keys
    # actually measure, or the UI reports the wrong dimension for real money.
    unit: str = UNIT_TOKENS


# Every Gemini model Vera can route to, priced INDIVIDUALLY rather than as one family.
# They differ materially — Langfuse's own table has gemini-2.5-flash at $3e-7/input
# token against gemini-3-flash-preview at $5e-7 — so a single family rate would be
# right for one model and wrong for the rest, with no way to tell from the trace.
#
# The cost of per-model entries is that a model NOT listed here matches nothing and
# renders blank cost. A catch-all `^gemini-.*$` alongside these would paper over that,
# but Langfuse resolves ties by `project_id ASC, start_date DESC NULLS LAST` — model
# name is not in the ordering — so which entry wins would hinge on start_date rather
# than on specificity. Cost accuracy should not rest on that, so the safety net is the
# coverage warning in main() instead: it names any configured model with no entry.
GEMINI_MODELS: tuple[str, ...] = (
    "gemini-2.5-flash",  # voice_llm_default_model, and the post-call eval
    "gemini-3.1-flash-lite",  # summary, health observer
    "gemini-3.5-flash",  # observer extraction
    "gemini-3.6-flash",  # seen live via a tenant llm_model_override
)


def _gemini_entry(model: str) -> ModelPrice:
    """One price entry for one Gemini model.

    The `(@version)?` suffix mirrors Langfuse's own built-in Gemini patterns, so a
    Vertex-pinned `model@20250101` still matches. The `cached` key is REQUIRED:
    without it Langfuse prices cache hits at $0 and understates cost — the mirror
    image of the bug the export-time correction fixes.
    """
    slug = model.upper().replace("-", "_").replace(".", "_")
    return ModelPrice(
        f"vera-{model}",
        rf"(?i)^{re.escape(model)}(@[a-zA-Z0-9]+)?$",
        {
            USAGE_INPUT: f"LANGFUSE_PRICE_LLM_{slug}_INPUT_PER_TOKEN",
            USAGE_OUTPUT: f"LANGFUSE_PRICE_LLM_{slug}_OUTPUT_PER_TOKEN",
            USAGE_CACHED: f"LANGFUSE_PRICE_LLM_{slug}_CACHED_PER_TOKEN",
        },
    )


MODELS: tuple[ModelPrice, ...] = (
    ModelPrice(
        "vera-deepgram-flux",
        r"(?i)^flux-.*$",
        {STT_AUDIO_MS: "LANGFUSE_PRICE_STT_FLUX_PER_MS"},
        UNIT_MILLISECONDS,
    ),
    ModelPrice(
        "vera-deepgram-nova",
        r"(?i)^nova-.*$",
        {STT_AUDIO_MS: "LANGFUSE_PRICE_STT_NOVA_PER_MS"},
        UNIT_MILLISECONDS,
    ),
    ModelPrice(
        "vera-cartesia-sonic",
        r"(?i)^sonic-.*$",
        {TTS_CHARACTERS: "LANGFUSE_PRICE_TTS_SONIC_PER_CHARACTER"},
        UNIT_CHARACTERS,
    ),
    *(_gemini_entry(model) for model in GEMINI_MODELS),
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
                value = float(raw)
            except ValueError:
                missing.append(env_var)
                continue
            # Zero/negative/inf/nan all parse fine but must never seed: $0.00 reads as
            # a free tier, and inf/nan would serialize to invalid JSON.
            if not math.isfinite(value) or value <= 0:
                missing.append(env_var)
                continue
            rates[env_var] = value
    if missing:
        raise MissingRateError(
            f"missing, unparseable, or non-positive rate env vars: {sorted(set(missing))}"
        )
    return rates


def matching_entry(model: str) -> ModelPrice | None:
    """The entry whose pattern matches *model*, or None when nothing prices it.

    Mirrors how Langfuse resolves a price, so the coverage check answers the same
    question the cost engine will: does this model have a price at all?
    """
    return next((m for m in MODELS if re.match(m.match_pattern, model)), None)


def build_payload(price: ModelPrice, *, rates: Mapping[str, float]) -> dict[str, Any]:
    """The POST body for one model entry.

    Prices go in `pricingTiers[0].prices` — the deprecated flat
    inputPrice/outputPrice cannot express a custom usage key at all. The tier must
    also be named, prioritised, carry its (empty) conditions and be flagged
    isDefault; the endpoint rejects the body outright otherwise.

    No `modelId`: this API has no upsert. POST rejects a duplicate `modelName` even
    when handed the existing id, and PUT/PATCH are 405 — so replacing an entry means
    deleting it first (see `seed`).
    """
    payload: dict[str, Any] = {
        "modelName": price.model_name,
        "matchPattern": price.match_pattern,
        "unit": price.unit,
        "pricingTiers": [
            {
                "name": _DEFAULT_TIER_NAME,
                "isDefault": True,
                "priority": 0,
                "conditions": [],
                "prices": {key: rates[env_var] for key, env_var in price.env_vars.items()},
            }
        ],
    }
    return payload


async def _existing_ids(client: httpx.AsyncClient) -> dict[str, str]:
    """Every model name -> id, across ALL pages.

    Paginating is not optional: the listing includes Langfuse's ~160 built-in
    entries alongside ours, so a single page misses the `vera-*` ones entirely.
    Missing them makes the upsert POST omit `modelId`, which Langfuse rejects on
    the (projectId, modelName) uniqueness check — the seeder then fails on its
    first re-run instead of being idempotent.
    """
    ids: dict[str, str] = {}
    for page in range(1, _MAX_MODEL_PAGES + 1):
        response = await client.get("/api/public/models", params={"limit": 100, "page": page})
        response.raise_for_status()
        data: list[dict[str, Any]] = response.json().get("data", [])
        if not data:
            break
        ids.update({m["modelName"]: m["id"] for m in data if "modelName" in m and "id" in m})
    return ids


async def seed(client: httpx.AsyncClient, rates: Mapping[str, float]) -> list[str]:
    """Upsert every entry; returns the model names written."""
    existing = await _existing_ids(client)
    written: list[str] = []
    for price in MODELS:
        if (model_id := existing.get(price.model_name)) is not None:
            # There is no update endpoint: POST rejects a duplicate modelName even
            # with the id, and PUT/PATCH are 405. Replacing means deleting first.
            # Rates are all resolved before any request is issued, so a delete is
            # only ever followed by a create that already has its values in hand.
            deleted = await client.delete(f"/api/public/models/{model_id}")
            if deleted.is_error:
                logger.error(
                    "could not replace %s (DELETE %s): %s",
                    price.model_name,
                    deleted.status_code,
                    deleted.text[:500],
                )
            deleted.raise_for_status()
            logger.info("replacing existing %s", price.model_name)
        payload = build_payload(price, rates=rates)
        response = await client.post("/api/public/models", json=payload)
        if response.is_error:
            # Surface the server's own explanation. `raise_for_status()` alone reports
            # only "400 Bad Request", which says nothing about WHICH field is wrong —
            # and this endpoint validates a fair few (unit, and every pricing-tier
            # field). The body names them; hiding it turns a 30-second fix into a hunt.
            logger.error(
                "POST %s failed (%s): %s",
                price.model_name,
                response.status_code,
                response.text[:1000],
            )
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
    # Strip any "provider:" prefix first: LLMSpec.parse splits it off before a span
    # ever sees it, so logging selectors verbatim would never match a pattern here.
    configured_selectors = {
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
    models = sorted({selector.rpartition(":")[2] for selector in configured_selectors})
    if unpriced := [m for m in models if not matching_entry(m)]:
        # The whole point of per-model entries: an unlisted model matches nothing and
        # renders blank cost, which reads as "this surface is free". Say so loudly here
        # rather than leaving it to be rediscovered in the UI as broken instrumentation.
        logger.warning(
            "NO PRICE ENTRY for these configured models — their observations will "
            "render BLANK cost: %s",
            unpriced,
        )
        logger.warning("add each to GEMINI_MODELS (or MODELS) and re-run with its rates")
    else:
        logger.info("every configured model matches a seeded entry: %s", models)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
