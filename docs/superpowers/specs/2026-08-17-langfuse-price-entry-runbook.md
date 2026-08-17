# Langfuse price entry runbook (manual fallback)

**Preferred path:** `just langfuse-seed-prices` (`vera-backend/scripts/seed_langfuse_prices.py`).
It is idempotent, reads rates from env vars, and refuses to write a `$0.00` entry. Use this
document only when the script isn't an option — no shell access to the target environment,
adjusting a single rate on a hand-provisioned project, or a one-off sanity check of what the
script would have written. If you find yourself reaching for this doc regularly, that's a
signal to fix whatever is blocking the script, not to keep hand-entering prices here — a doc
that quietly becomes the primary route drifts from the seeder's `MODELS` tuple the first time
either one changes without the other.

## Why this exists at all

Vera's spans carry raw usage (`stt_audio_ms`, `tts_characters`, `input`/`output`/`cached` token
counts) and nothing else — Vera holds no prices anywhere in the app. Langfuse only computes a
cost when a model definition exists whose `matchPattern` matches the ingested model name **and**
whose `pricingTiers[0].prices` has an entry for every usage key the span sends. Without that
match, the usage still ingests and shows up in the UI — it just renders a **blank** cost, which
looks exactly like broken instrumentation instead of "missing price entry."

## The click path

1. Open the Langfuse web UI and select the project the calls are landing in.
2. **Settings → Models → + New model.**
3. Fill in the form. The UI's field names are not the API's field names — map them like this:

   | UI field | API field | Notes |
   |---|---|---|
   | Model name | `modelName` | Exact string from the table below, e.g. `vera-<gemini-model>` (one entry PER MODEL). |
   | Match pattern | `matchPattern` | A regex tested against the ingested model name. Use the family pattern as-is — do not narrow it to one version (see "family patterns" note below). |
   | Match config → case-insensitive | (embedded in `matchPattern`) | The patterns below already start with `(?i)`; leave the UI's own case-insensitive toggle off to avoid double-applying it, or drop the `(?i)` prefix if the UI applies it for you — check the resulting match either way (see "discover the usage keys" below for how to test). |
   | Prices → usage type / unit | key inside `pricingTiers[0].prices` | This is a **free-text key**, not a dropdown of "input/output tokens" — type the exact usage key from the table (e.g. `stt_audio_ms`), not a token-billing label. |
   | Prices → price | the value at that key | A plain decimal, in USD. See the unit warning below for the audio rows. |

   Do **not** use the deprecated flat "Input price" / "Output price" / "Total price" fields if the
   UI still shows them — those map to the legacy `inputPrice`/`outputPrice`/`totalPrice` API
   fields, which cannot express a custom usage key like `stt_audio_ms` at all. Always use the
   pricing-tiers table so the key is explicit.

4. Save. Repeat for each row below. If a `modelName` already exists, editing and re-saving is the
   UI's equivalent of the seeder's modelId-threaded upsert — you're updating the same entry, not
   creating a duplicate.

## The four entries

Copied from `MODELS` in `vera-backend/scripts/seed_langfuse_prices.py` — that file is the source
of truth; if this table and the script ever disagree, the script wins and this table is stale.

| `modelName` | `matchPattern` | usage key(s) | price | rate env var (script) |
|---|---|---|---|---|
| `vera-deepgram-flux` | `(?i)^flux-.*$` | `stt_audio_ms` | **per millisecond** — see warning below | `LANGFUSE_PRICE_STT_FLUX_PER_MS` |
| `vera-deepgram-nova` | `(?i)^nova-.*$` | `stt_audio_ms` | **per millisecond** — see warning below | `LANGFUSE_PRICE_STT_NOVA_PER_MS` |
| `vera-cartesia-sonic` | `(?i)^sonic-.*$` | `tts_characters` | per character | `LANGFUSE_PRICE_TTS_SONIC_PER_CHARACTER` |
| `vera-<gemini-model>` (one entry PER MODEL) | `(?i)^gemini-.*$` | `input`, `output`, `cached` (all three — see note below) | per token, one price per key | `LANGFUSE_PRICE_LLM_GEMINI_INPUT_PER_TOKEN`, `LANGFUSE_PRICE_LLM_GEMINI_OUTPUT_PER_TOKEN`, `LANGFUSE_PRICE_LLM_GEMINI_CACHED_PER_TOKEN` |

**Family patterns, not exact versions.** `(?i)^sonic-.*$` matches the pinned
`sonic-3.5-2026-05-04` today and will still match `sonic-4` after a bump. Do not narrow a pattern
to an exact model string — an exact pattern silently zeros cost on the next model-family change,
and a missing match renders identically to "no cost data," which is invisible in the UI.

### Unit warning — the audio rows are PER MILLISECOND, not per minute

Langfuse stores usage as integers, so Vera reports STT audio duration in **whole
milliseconds**. The price entry must therefore be **dollars per millisecond**, matching that
unit exactly.

Published Deepgram rates are quoted **per minute**. Converting a per-minute list price to the
per-millisecond figure this field needs is:

```
per_millisecond_rate = per_minute_rate / 60000
```

If you instead type the per-minute figure directly into the per-millisecond field, the displayed
cost is **60,000× too high** — and because $0.0077 and $0.0077 both look like plausible small
dollar amounts, this mistake does not look wrong in the UI. Always run the division before
entering the number, and sanity-check the result against a real call (see "check the arithmetic
by hand," below).

### The Gemini entry needs all three keys — `input`, `output`, AND `cached`

Vera separates cached (already-billed-cheaper) input tokens from fresh input tokens and reports
them under a distinct `cached` usage key so cache hits aren't double-billed at the full input
rate. If the Gemini model entry only has `input` and `output` prices — which is exactly what a
built-in / legacy Langfuse entry predating custom usage keys will have — the `cached` tokens
price at **$0**, silently **understating** cost. This is the mirror image of the overstatement
bug this whole effort corrects, and just as invisible: nothing in the UI flags a missing price
key, the total for that generation is just quietly too low. Always add `cached` explicitly,
even if you're only touching this entry to bump `input`/`output`.

## How to discover the usage keys from a live observation (don't just trust this doc)

This table can go stale if the instrumentation's attribute names ever change. Before trusting it
blindly, verify against a real trace:

1. In Langfuse, open any recent trace for a call.
2. Find an observation named `vera.stt.usage` or `vera.tts.usage` (or any Gemini `generation` —
   e.g. the cascade's LLM turn, the post-call eval, or the call summary).
3. Open its details and look at **Usage** / the raw `usage_details` — the keys shown there
   (`stt_audio_ms`, `tts_characters`, `input`, `output`, `cached`) are the exact strings the price
   entry's `pricingTiers[0].prices` must match. If a key here doesn't match this doc, the code
   changed and this doc needs an update — file it, don't just work around it by guessing.

## "Cost is blank — why?" triage

All five of these causes render **identically** in the Langfuse UI: a `generation` observation
with usage details attached but no `$` cost, or a `$0.00` that could be a real free tier or a
missing price. Work through them in order:

| # | Cause | How to check |
|---|---|---|
| 1 | **No model entry exists** for this model name at all | Settings → Models — search for the ingested model name family (e.g. `flux`, `gemini`). If nothing matches, that's it. |
| 2 | **`matchPattern` doesn't match the ingested model name** | Copy the exact model name string from the observation (step 2 above) and test it against the entry's regex (a quick Python `re.match(pattern, name)` or an online regex tester). Case, a stray anchor, or a missed prefix (`flux-general-en` vs. `flux_general_en`) are the usual culprits. |
| 3 | **Price key ≠ usage key** | Open the model entry's pricing tiers and diff the key names character-for-character against the observation's `usage_details` keys (step 2/3 above). `stt_audio_ms` vs. `stt_audio_seconds`, or a missing `cached` key, are exactly this failure. |
| 4 | **Usage key typo'd in the instrumentation itself** | If the model entry's keys look right but still don't price, check `vera_core/observability/usage_spans.py` (and `llm_usage_export.py`) for the actual constant strings being emitted — a code-side rename that didn't also update the seeded entries lands here. |
| 5 | **The observation is a span, not a `generation`/`embedding`** | Only `generation` and `embedding` observation types carry cost in Langfuse — a plain `span` never prices, no matter how correct its usage details and the model entry are. Check the observation's type in the UI (or `langfuse.observation.type` in the raw attributes). |

## Public list prices — sanity reference only, never the contracted rate

Use these only to sanity-check that a computed per-millisecond / per-character rate is in a
plausible ballpark — never enter a public list price as the actual seeded rate. Vera's real
rates are contract-specific and must come from the actual vendor agreement.

- Deepgram Nova (streaming): ~$0.0077/minute → ~$0.00000012833/ms
- Deepgram Flux: ~$0.0065/minute → ~$0.00000010833/ms
- Cartesia Sonic: roughly $5–$37 per million characters, depending on tier → $0.000005–$0.000037/character

If your computed per-millisecond or per-character rate is off from these by several orders of
magnitude, you likely have a unit conversion error (see the unit warning above) rather than an
unusually priced contract.


## Gemini is priced per model, not per family

Every Gemini model Vera routes to gets its OWN entry — `vera-gemini-2.5-flash`,
`vera-gemini-3.1-flash-lite`, `vera-gemini-3.5-flash`, `vera-gemini-3.6-flash` —
because their rates differ by roughly 2x and one family rate would be right for one
model and wrong for the rest, with nothing in the trace to say which.

Deepgram and Cartesia stay FAMILY-matched (`^flux-.*$`, `^nova-.*$`, `^sonic-.*$`)
on purpose: their version bumps are rate-compatible, so a family pattern there
prevents a bump from silently zeroing cost.

**The tradeoff to know:** a Gemini model that is not listed matches nothing and
renders blank cost. There is deliberately no catch-all — Langfuse resolves ties by
`project_id ASC, start_date DESC NULLS LAST` and model name is not in the ordering,
so which entry won would hinge on `start_date` rather than on specificity. The
safety net is the seeder's own coverage check: it WARNS, naming any configured model
with no price entry. If you see that warning, add the model to `GEMINI_MODELS` and
re-run with its three rates.

**Flux and Nova are separately priced** (`LANGFUSE_PRICE_STT_FLUX_PER_MS` vs
`LANGFUSE_PRICE_STT_NOVA_PER_MS`). Deepgram lists them at different rates — Flux
~$0.0065/min, Nova ~$0.0077/min streaming — so giving both the same value silently
misprices whichever one is wrong.
