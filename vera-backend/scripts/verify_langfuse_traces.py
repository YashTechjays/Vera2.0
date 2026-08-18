"""Verify that a real call's cost actually landed in Langfuse.

Unit tests prove what Vera EMITS. They cannot prove Langfuse ingested it, typed it
as a generation, matched a price entry and rendered a number. Only a live call does
that, and this is the check for it — the design makes that pass the definition of
done, so it should be a command rather than a click-through.

    just langfuse-verify              # newest call trace
    just langfuse-verify <trace-id>   # a specific one

Exits non-zero when something is wrong, so it works as a gate. What it checks:

  1. every model Vera routes to has a price entry (else blank cost, which reads
     as "this surface is free") — except the tiers in the seeder's KNOWN_UNPRICED,
     which are deliberately unpriced and reported without failing the run;
  2. every GENERATION carries a non-blank cost — checked across all of them, since
     an unpriced model shows on some spans and not others;
  3. usage reconciles: `input + cached` equals the provider's own prompt count, so
     nothing billed was invented or dropped;
  4. the control-plane spans (post-call eval, summary, whisper) share the call's
     trace, which is what makes a per-call total real;
  5. cache-hit ratios per model, so caching regressions are visible, and the derived
     thinking-token count, which nothing else reconciles.

PHI: reads span names, models, token counts and cost. It never prints span input,
output, or metadata, all of which can carry transcript text on the SDK's own spans.
"""

import asyncio
import base64
import re
import sys
from collections import defaultdict
from typing import Any

import httpx

from scripts.seed_langfuse_prices import (
    KNOWN_UNPRICED,
    MODELS,
    configured_models,
    existing_models,
    matching_entry,
)
from vera_core.config import get_settings
from vera_core.observability.llm_usage_export import THINKING_TOKENS_ATTR
from vera_core.observability.usage_spans import (
    CANCELLED_ATTR,
    SPAN_STT_USAGE,
    SPAN_TTS_USAGE,
    TTS_CHARACTERS,
    USAGE_CACHED,
    USAGE_INPUT,
)

# Spans the control plane emits. Their presence IN the call's trace is the whole
# point of the cross-process trace link; in a separate trace they are orphans.
CONTROL_PLANE_SPANS = ("vera.post_call.eval", "vera.call_summary", "vera.coaching.whisper")

# The worker's root span for a call — used to find the newest call trace.
CALL_ROOT_SPAN = "job_entrypoint"


def _auth(settings: Any) -> str:
    raw = f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _matches(pattern: str, model: str) -> bool:
    """Whether a Langfuse matchPattern would price *model*. A built-in with a pattern
    this regex engine rejects is treated as no match rather than crashing the gate."""
    try:
        return re.match(pattern, model) is not None
    except re.error:
        return False


def _attrs(observation: dict[str, Any]) -> dict[str, Any]:
    """One accessor for the Langfuse response shape, so a change to it is one edit."""
    attrs: dict[str, Any] = (observation.get("metadata") or {}).get("attributes") or {}
    return attrs


def _usage(observation: dict[str, Any], key: str) -> int:
    return int((observation.get("usageDetails") or {}).get(key, 0))


def reconciles(observation: dict[str, Any]) -> bool | None:
    """True when `input + cached` equals the SDK's own prompt count, None when the
    span carries no SDK count to compare against (our own usage spans, and any
    provider that reports no token usage)."""
    reported = _attrs(observation).get("gen_ai.usage.input_tokens")
    if reported is None or USAGE_INPUT not in (observation.get("usageDetails") or {}):
        return None
    return _usage(observation, USAGE_INPUT) + _usage(observation, USAGE_CACHED) == int(reported)


def _report_cancelled_tts(obs: list[dict[str, Any]]) -> None:
    """How many synthesized characters were charged on a CANCELLED request.

    `TTSMetrics.characters_count` is `len(input_text)` — the text handed to the
    synthesizer, not what was rendered — so a barge-in still bills the whole utterance.
    Whether that matches what Cartesia actually charges depends on how much of the text
    reached them before the cancel, which no test can settle. Print the exposure so it
    can be reconciled against one invoice instead of assumed either way.
    """
    tts = [o for o in obs if o.get("name") == SPAN_TTS_USAGE]
    cancelled = [o for o in tts if _attrs(o).get(CANCELLED_ATTR)]
    if not cancelled:
        return
    chars = sum(_usage(o, TTS_CHARACTERS) for o in cancelled)
    total = sum(_usage(o, TTS_CHARACTERS) for o in tts)
    share = f"{chars / total:.0%}" if total else "n/a"
    print(f"\n  {len(cancelled)} cancelled TTS request(s): {chars} of {total} characters ({share})")
    print("  billed in full — reconcile against a Cartesia invoice before trusting the TTS line")


def _thinking(observation: dict[str, Any]) -> int:
    """The thinking tokens the export-time correction derived for this span, 0 when it
    derived none (or the span is not one it touched)."""
    return int(_attrs(observation).get(THINKING_TOKENS_ATTR, 0) or 0)


async def _newest_call_trace(client: httpx.AsyncClient) -> str | None:
    response = await client.get("/api/public/traces", params={"limit": 50})
    response.raise_for_status()
    for trace in response.json().get("data", []):
        if trace.get("name") == CALL_ROOT_SPAN:
            trace_id: str = trace["id"]
            return trace_id
    return None


async def _price_entry_report(client: httpx.AsyncClient, settings: Any) -> tuple[bool, bool]:
    """(all Vera entries exist, every configured model is covered).

    Kept separate because they mean different things: the first says the seeder ran,
    the second says nothing Vera routes to is unpriced. A blank cost is only a
    "go seed" instruction when the first is False."""
    seeded = await existing_models(client)

    print("\n=== PRICE ENTRIES ===")
    missing_entries = [m.model_name for m in MODELS if m.model_name not in seeded]
    for model in MODELS:
        mark = "ok " if model.model_name in seeded else "MISSING"
        print(f"  [{mark:>7}] {model.model_name}")
    if missing_entries:
        print(f"  -> run `just langfuse-seed-prices`; missing: {missing_entries}")

    # Asked of the LIVE listing, not just Vera's own MODELS: Langfuse ships ~160
    # built-in entries that price several of the models Vera routes to, so the offline
    # check in the seeder can only ever answer "did WE price it". The real question a
    # gate should ask is "will this render a number", and the listing answers it.
    patterns = [p for p in (m.get("matchPattern") for m in seeded.values()) if p]
    unpriced: list[str] = []
    by_langfuse: list[str] = []
    for configured in configured_models(settings):
        if matching_entry(configured) is not None:
            continue
        if any(_matches(pattern, configured) for pattern in patterns):
            by_langfuse.append(configured)
        elif configured not in KNOWN_UNPRICED:
            unpriced.append(configured)

    if by_langfuse:
        print(f"\n  priced by a Langfuse built-in entry, not by us: {by_langfuse}")
    if known := [m for m in configured_models(settings) if m in KNOWN_UNPRICED]:
        # Not a failure: deliberately unpriced fallback tiers (adr/devops-todo.md #23).
        # A gate that is red on a healthy system is one everyone learns to ignore.
        print(f"\n  knowingly unpriced, nothing prices these (not a failure): {known}")
    if unpriced:
        print(f"\n  NO PRICE ENTRY for configured models: {unpriced}")
        print("  their observations will render BLANK cost, which looks like broken tracing")
    return not missing_entries, not unpriced


def _report_trace(full: dict[str, Any], *, prices_ok: bool) -> bool:
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
        cached = sum(_usage(o, USAGE_CACHED) for o in group)
        prompt = cached + sum(_usage(o, USAGE_INPUT) for o in group)
        ratio = f"{cached / prompt:.0%}" if prompt else "n/a"
        flag = "" if priced == len(group) else f"   <-- {len(group) - priced} BLANK"
        # Thinking is a RESIDUAL (total - prompt - completion) billed at the output
        # rate, so anything else the provider folds into its total lands here too and
        # costs ~8x what input would. `input + cached` reconciles below; nothing
        # reconciles output, so surface the derived number and eyeball it.
        thinking = sum(_thinking(o) for o in group)
        derived = f" thinking={thinking}" if thinking else ""
        print(
            f"    {model:28} n={len(group):4} priced={priced:4} ${cost:<12.6f} "
            f"cache={ratio}{derived}{flag}"
        )

    mismatched = [o for o in billable if reconciles(o) is False]
    if mismatched:
        print(f"\n  USAGE DOES NOT RECONCILE on {len(mismatched)} generation(s):")
        for o in mismatched[:5]:
            print(f"    {o.get('name')} {o.get('usageDetails')}")
        print("    input + cached must equal the provider's own prompt-token count")

    _report_cancelled_tts(obs)

    names = {str(o.get("name")) for o in obs}
    joined = sorted(n for n in CONTROL_PLANE_SPANS if n in names)
    print(
        f"\n  vera usage spans: {sorted(n for n in (SPAN_STT_USAGE, SPAN_TTS_USAGE) if n in names)}"
    )
    print(f"  control-plane spans in THIS trace: {joined or 'none'}")
    if not joined:
        print("    (none ran, or they formed their own trace — exercise whisper/summary")
        print("     and let post-call eval run, then re-check)")

    if blank and not prices_ok:
        print(f"\n  {len(blank)} generation(s) with BLANK cost — seed the missing model entries")
    elif blank:
        # Every entry exists, so the gap is chronological rather than configuration:
        # Langfuse computes cost at INGESTION and stores it. Seeding afterwards never
        # backfills. Saying "seed the missing entries" here would send someone to
        # re-run a seeder that is already correct.
        print(f"\n  {len(blank)} generation(s) with BLANK cost, but every price entry EXISTS.")
        print("  This trace was ingested BEFORE those entries were seeded — Langfuse")
        print("  prices at ingestion time and does not backfill. Place a new call.")
    return not blank and not mismatched


async def main() -> int:
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
        entries_present, all_covered = await _price_entry_report(client, settings)

        trace_id = wanted or await _newest_call_trace(client)
        if trace_id is None:
            print(f"\nno `{CALL_ROOT_SPAN}` trace found — place a call first.")
            return 1
        response = await client.get(f"/api/public/traces/{trace_id}")
        response.raise_for_status()
        trace_ok = _report_trace(response.json(), prices_ok=entries_present)

    ok = entries_present and all_covered and trace_ok
    print("\n" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
