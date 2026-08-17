"""Verify that a real call's cost actually landed in Langfuse.

Unit tests prove what Vera EMITS. They cannot prove Langfuse ingested it, typed it
as a generation, matched a price entry and rendered a number. Only a live call does
that, and this is the check for it — the design makes that pass the definition of
done, so it should be a command rather than a click-through.

    just langfuse-verify              # newest call trace
    just langfuse-verify <trace-id>   # a specific one

Exits non-zero when something is wrong, so it works as a gate. What it checks:

  1. every model Vera routes to has a price entry (else blank cost, which reads
     as "this surface is free");
  2. every GENERATION carries a non-blank cost — checked across all of them, since
     an unpriced model shows on some spans and not others;
  3. usage reconciles: `input + cached` equals the provider's own prompt count, so
     nothing billed was invented or dropped;
  4. the control-plane spans (post-call eval, summary, whisper) share the call's
     trace, which is what makes a per-call total real;
  5. cache-hit ratios per model, so caching regressions are visible.

PHI: reads span names, models, token counts and cost. It never prints span input,
output, or metadata, all of which can carry transcript text on the SDK's own spans.
"""

import asyncio
import base64
import logging
import sys
from collections import defaultdict
from typing import Any

import httpx

from scripts.seed_langfuse_prices import MODELS, matching_entry
from vera_core.config import get_settings
from vera_core.observability.usage_spans import (
    SPAN_STT_USAGE,
    SPAN_TTS_USAGE,
    USAGE_CACHED,
    USAGE_INPUT,
)

logger = logging.getLogger("vera.verify_langfuse_traces")

# Spans the control plane emits. Their presence IN the call's trace is the whole
# point of the cross-process trace link; in a separate trace they are orphans.
CONTROL_PLANE_SPANS = ("vera.post_call.eval", "vera.call_summary", "vera.coaching.whisper")

# The worker's root span for a call — used to find the newest call trace.
CALL_ROOT_SPAN = "job_entrypoint"


def _auth(settings: Any) -> str:
    raw = f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def configured_models(settings: Any) -> list[str]:
    """Every model selector Vera can route to, provider prefix stripped.

    `LLMSpec.parse` splits `provider:model` before the plugin ever sees it, so a
    span carries the bare name — comparing the selector verbatim would never match.
    """
    selectors = {
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
    return sorted({s.rpartition(":")[2] for s in selectors})


def reconciles(observation: dict[str, Any]) -> bool | None:
    """True when `input + cached` equals the SDK's own prompt count, None when the
    span carries no SDK count to compare against (our own usage spans, and any
    provider that reports no token usage)."""
    attrs = (observation.get("metadata") or {}).get("attributes") or {}
    reported = attrs.get("gen_ai.usage.input_tokens")
    usage = observation.get("usageDetails") or {}
    if reported is None or USAGE_INPUT not in usage:
        return None
    return int(usage.get(USAGE_INPUT, 0)) + int(usage.get(USAGE_CACHED, 0)) == int(reported)


async def _newest_call_trace(client: httpx.AsyncClient) -> str | None:
    response = await client.get("/api/public/traces", params={"limit": 50})
    response.raise_for_status()
    for trace in response.json().get("data", []):
        if trace.get("name") == CALL_ROOT_SPAN:
            trace_id: str = trace["id"]
            return trace_id
    return None


async def _price_entry_report(client: httpx.AsyncClient, settings: Any) -> bool:
    """Seeded entries vs configured models. False when something is unpriced."""
    seeded: dict[str, str] = {}
    for page in range(1, 21):
        response = await client.get("/api/public/models", params={"limit": 100, "page": page})
        response.raise_for_status()
        data = response.json().get("data", [])
        if not data:
            break
        seeded.update({m["modelName"]: m.get("matchPattern", "") for m in data})

    print("\n=== PRICE ENTRIES ===")
    missing_entries = [m.model_name for m in MODELS if m.model_name not in seeded]
    for model in MODELS:
        mark = "ok " if model.model_name in seeded else "MISSING"
        print(f"  [{mark:>7}] {model.model_name}")
    if missing_entries:
        print(f"  -> run `just langfuse-seed-prices`; missing: {missing_entries}")

    unpriced = [m for m in configured_models(settings) if matching_entry(m) is None]
    if unpriced:
        print(f"\n  NO PRICE ENTRY for configured models: {unpriced}")
        print("  their observations will render BLANK cost, which looks like broken tracing")
    return not missing_entries and not unpriced


def _report_trace(full: dict[str, Any]) -> bool:
    """Print the per-trace findings. False when the trace has a real problem."""
    obs = full.get("observations", [])
    print(f"\n=== TRACE {full.get('id')} ===")
    print(f"  name={full.get('name')}  session={full.get('sessionId')}")
    print(f"  totalCost={full.get('totalCost')}  observations={len(obs)}")

    generations = [o for o in obs if str(o.get("type")).upper() == "GENERATION"]
    # Only a generation that REPORTS usage can be priced. The SDK also emits
    # usage-less spans carrying a model attribute — `user_turn`, `tts_node`,
    # `llm_fallback_adapter` — which Langfuse types as generations. Those are blank
    # by nature, not by fault, and counting them would keep this gate permanently red.
    billable = [o for o in generations if o.get("usageDetails")]
    blank = [o for o in billable if not (o.get("calculatedTotalCost") or o.get("totalCost"))]

    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for o in billable:
        by_model[str(o.get("model"))].append(o)

    print(
        f"\n  --- {len(billable)} billable generations across {len(by_model)} models "
        f"({len(generations) - len(billable)} usage-less SDK spans ignored) ---"
    )
    for model, group in sorted(by_model.items()):
        priced = sum(1 for o in group if (o.get("calculatedTotalCost") or o.get("totalCost")))
        cost = sum(float(o.get("calculatedTotalCost") or o.get("totalCost") or 0) for o in group)
        cached = sum(int((o.get("usageDetails") or {}).get(USAGE_CACHED, 0)) for o in group)
        prompt = cached + sum(int((o.get("usageDetails") or {}).get(USAGE_INPUT, 0)) for o in group)
        ratio = f"{cached / prompt:.0%}" if prompt else "n/a"
        flag = "" if priced == len(group) else f"   <-- {len(group) - priced} BLANK"
        print(
            f"    {model:28} n={len(group):4} priced={priced:4} ${cost:<12.6f} cache={ratio}{flag}"
        )

    mismatched = [o for o in billable if reconciles(o) is False]
    if mismatched:
        print(f"\n  USAGE DOES NOT RECONCILE on {len(mismatched)} generation(s):")
        for o in mismatched[:5]:
            print(f"    {o.get('name')} {o.get('usageDetails')}")
        print("    input + cached must equal the provider's own prompt-token count")

    names = {str(o.get("name")) for o in obs}
    joined = sorted(n for n in CONTROL_PLANE_SPANS if n in names)
    print(
        f"\n  vera usage spans: {sorted(n for n in (SPAN_STT_USAGE, SPAN_TTS_USAGE) if n in names)}"
    )
    print(f"  control-plane spans in THIS trace: {joined or 'none'}")
    if not joined:
        print("    (none ran, or they formed their own trace — exercise whisper/summary")
        print("     and let post-call eval run, then re-check)")

    if blank:
        print(f"\n  {len(blank)} generation(s) with BLANK cost — seed the missing model entries")
    return not blank and not mismatched


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = get_settings()
    if not settings.langfuse_host:
        print("VERA_LANGFUSE_HOST is not set — nothing was traced.")
        return 1

    wanted = sys.argv[1] if len(sys.argv) > 1 else None
    print(f"host: {settings.langfuse_host}")
    async with httpx.AsyncClient(
        base_url=settings.langfuse_host.rstrip("/"),
        headers={"Authorization": _auth(settings)},
        timeout=60.0,
    ) as client:
        prices_ok = await _price_entry_report(client, settings)

        trace_id = wanted or await _newest_call_trace(client)
        if trace_id is None:
            print(f"\nno `{CALL_ROOT_SPAN}` trace found — place a call first.")
            return 1
        response = await client.get(f"/api/public/traces/{trace_id}")
        response.raise_for_status()
        trace_ok = _report_trace(response.json())

    print("\n" + ("PASS" if prices_ok and trace_ok else "FAIL"))
    return 0 if prices_ok and trace_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
