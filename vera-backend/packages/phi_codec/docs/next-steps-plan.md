# Next-steps plan — two focused passes

Two independent follow-ups to the prototype. **Recommended order: B before A** — B is
small, low-risk, and fixes a known correctness bug; A is a larger taxonomy expansion.

---

## Pass B — Proximity-weighted context (small, do first)

### Problem
Presidio's default `LemmaContextAwareEnhancer` boosts a match if a context word appears
*anywhere* in the token window, ignoring distance. In
`"The member id is 244523 … you can reach the patient at 919912345"`, the far-away
"member/group" words bleed across the sentence and outscore the adjacent "reach … at"
cue, so the contact number types as `MEMBER_ID` instead of `PHONE`. (Tokenized, so not a
leak — a typing bug.)

### Approach
Subclass Presidio's enhancer to decay the boost by token distance to the nearest context
word. Inject it into the analyzer; no recognizer changes.

- **New file** `phi_codec/detection/proximity_enhancer.py`: `ProximityContextEnhancer(LemmaContextAwareEnhancer)`.
  Override the context-match step so the boost is `factor * max(0, 1 - dist/window)`,
  where `dist` is the token gap between the result span and the closest matching context
  lemma (full boost within ~3 tokens, fading to 0 by ~12). Keep the existing
  `min_score_with_context_similarity` floor behavior.
- **`detection/engine.py`**: pass `context_aware_enhancer=ProximityContextEnhancer(...)`
  to `AnalyzerEngine(...)`.

### Verification
- New `tests/test_proximity_context.py`: the mixed sentence above → `919912345` types as
  `PHONE`; `244523` stays `MEMBER_ID`; a clean `"callback number is …"` still types `PHONE`.
- Re-run `python -m phi_codec.eval.recall --n 300` (and `--no-gliner`): redaction/type
  recall must stay 100%, 0 leaks — synth context is adjacent, so proximity only helps.

### Risk / effort
Low. One class + injection + tests. Main risk is over-decaying legitimate boosts; the
recall harness is the guardrail. ~Half a pass.

---

## Pass A — Safe Harbor taxonomy alignment + missing identifier types (larger)

### Goal
Rename to the exact Safe Harbor token names and add the identifier types we don't yet
cover, so the codec maps 1:1 to the §3.3 table. Tokens stay `[[TYPE_N]]` (double-bracket,
order-stable) — only the TYPE strings align to the table.

### Type taxonomy changes (`config.py` `EntityType` + `PRESIDIO_LABEL_MAP`)
| Table # | Token name | Action |
|---|---|---|
| 2 | `STREET_ADDRESS`, `CITY`, `ZIP_CODE` | split ADDRESS→STREET_ADDRESS(street regex)+CITY(spaCy LOCATION); rename ZIP→ZIP_CODE; keep 2-letter **state** untokenized |
| 3 | `DATE`, `AGE_OVER_89` | keep DATE (year retained); add age>89 generalization |
| 5 | `FAX` | new — phone-shaped number with "fax" context |
| 9 | `BENEFICIARY_ID` | rename MEMBER_ID→BENEFICIARY_ID (MBI stays a subtype) |
| 11 | `LICENSE` | new — map built-in `US_NPI` + `MEDICAL_LICENSE`→LICENSE; add DEA/"MD-#####" |
| 12 | `VEHICLE` | new — VIN (17 alnum), plates, "unit T-44" |
| 13 | `DEVICE_SERIAL` | new — "serial/SN" anchored alnum; keep model string |
| 14 | `URL` | new — `https?://…`, `www.…` |
| 15 | `IP_ADDRESS` | new — IPv4 (+ optional IPv6) |
| 18 | `UNIQUE_CODE` | new catch-all — prefixed dashed codes ("PA-44129-X") w/ auth/claim/ref context |

Unchanged: `NAME`, `DATE`, `PHONE`, `SSN`, `MRN`, `ACCOUNT`, `EMAIL`, `MBI`.
**Out of scope (gateway, not text codec):** #16 biometric audio, #17 face photos — handled
by rejecting/stripping audio & image attachments upstream; documented, not implemented here.

### Recognizers (`detection/recognizers.py`)
New high-precision regex recognizers: URL, IP_ADDRESS, FAX (phone patterns + "fax"
context, ranked above PHONE), LICENSE (DEA/medical-license patterns), VEHICLE (VIN +
"unit/plate/ambulance" context), DEVICE_SERIAL ("serial:"-anchored), UNIQUE_CODE
(prefixed dashed alnum + auth/claim context), AGE_OVER_89 (`\b(9\d|1\d\d)\b` + "years
old/age"). Reuse the existing street-address recognizer → relabel `STREET_ADDRESS`.

### Supporting changes
- **`detection/engine.py`**: CITY-vs-STREET disambiguation (street regex wins →
  STREET_ADDRESS; remaining spaCy LOCATION → CITY); map NPI/MEDICAL_LICENSE → LICENSE.
- **`tokens/tokenizer.py`**: add the new types to the `_SPECIFICITY` tie-break table
  (e.g. URL/IP/EMAIL high; UNIQUE_CODE low so specific types win).
- **`formatting/tts.py`**: spell-out set for LICENSE/VEHICLE/DEVICE_SERIAL/UNIQUE_CODE;
  URL/EMAIL/CITY read naturally.
- **`eval/synth.py`**: generators + ground truth for the new types; extend recall report.
- **Tests**: rename MEMBER_ID→BENEFICIARY_ID / ZIP→ZIP_CODE across the suite; add a
  per-type recognizer test file; extend recall gate to the new types.
- **`ui/`**: `ET_COLORS` for new types; add example templates (URL/IP/fax/license/device/
  vehicle/unique-code/full-address); vault unaffected.
- **`README.md`**: add the Safe Harbor mapping table.

### Verification
- `uv run pytest` green after the rename.
- `python -m phi_codec.eval.recall --n 400`: new types ≥99% redaction recall, 0 leaks.
- UI smoke: each new template tokenizes to the correctly-named token and round-trips.

### Risk / effort
Medium. Mechanical rename touches many files; the fuzzy recognizers (UNIQUE_CODE, VEHICLE,
DEVICE_SERIAL) need precision tuning to avoid over-redaction. ~1.5–2 passes.

---

## Recommendation
Ship **Pass B first** (fast correctness win), then **Pass A**. They don't conflict, so
either can go first; A is just bigger. Both keep the invariants: recall-first, fail-closed
re-identify, leak canary, `[[TYPE_N]]` syntax.
