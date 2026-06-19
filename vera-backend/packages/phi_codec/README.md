# phi_codec — PHI de-identification / re-identification codec

A bidirectional PHI codec for a HIPAA-compliant, real-time voice AI agent (payer
eligibility / prior-auth calls). It tokenizes raw STT text **before** the LLM and
re-identifies LLM output **before** TTS and before the deterministic payer-API
connector — so the LLM and observability only ever see tokenized data.

```
Deepgram STT ──tokenize──▶ Gemini ──reidentify──▶ Cartesia TTS
  (raw PHI)                (tokens)   │            (spoken raw)
                                      └─reidentify_args─▶ payer API (exact raw)
```

Tokens are stable, semantically typed: `[[NAME_1]]`, `[[MEMBER_ID_1]]`, `[[SSN_1]]`.
Same entity → same token within a session. Re-identification is exact and lossless.

## Design

See the approved plan at `~/.claude/plans/i-need-to-explore-radiant-quail.md` for the
full architecture, NER library comparison, failure modes, and observability hooks.

Highlights:
- **Hybrid NER (Presidio):** regex/`PatternRecognizer`s carry the recall load for the
  structured identifiers that dominate payer calls (member ID, SSN, MRN, NPI, MBI,
  phone, account, ZIP); GLiNER (`urchade/gliner_multi_pii-v1`) handles free-text
  names/locations. Swappable `NerBackend` so a GPU sidecar can replace in-process later.
  **On the real-time voice path GLiNER is off by default** (~58ms vs ~3ms); seeding
  covers the patient's free-text PHI deterministically — see [Runtime mode](#runtime-mode-voice-pipeline).
- **Spoken-form normalization** runs before detection: collapses "X Y Z nine eight
  seven", NATO phonetic ("B as in boy"), digit-words, multipliers into canonical form.
- **Asymmetric latency:** tokenize runs detection (p95 ~2.5ms regex / ~58ms +GLiNER);
  re-identify is vault lookup + substitution (~0.1ms). Detection timeout falls back to
  regex-only (never to "no detection", which would leak).
- **Leak canary** scans tokenized text for residual PHI shapes — the backstop against
  the worst-case failure (PHI reaching the LLM untokenized).
- **Vault:** per-session, raw values encrypted at rest (Fernet stand-in for KMS
  envelope), per-session lock (Presidio mapping isn't thread-safe). Append-only audit
  log records token events with no plaintext PHI.
- **Known-value seeding (match-known + detect-unknown):** a provider initiates an
  eligibility call about a *known* patient, so the patient record (name, DOB, member ID,
  SSN, address) is pre-seeded into the session and its tokens minted before the call.
  Seeded values match deterministically (tier-0, score 1.0) ahead of the recognizers —
  STT's "X Y Z nine eight seven…" normalizes to the known member ID and matches exactly,
  and re-identification returns the exact record value, not the STT transcription. The
  regex/GLiNER tiers remain the backstop for PHI the payer introduces (names, auth codes,
  fax) that isn't in the seed. `await codec.seed_session(sid, {"NAME": "John Smith", ...})`.
  Seed keys may be canonical types, **our maintained aliases** of common EHR field names
  (`MEMBER_ID`/`GROUP_NUMBER`→`BENEFICIARY_ID`, `DOB`→`DATE`, `PHONE_NUMBER`→`PHONE`), or
  token-style `TYPE_N` keys; multiple values of one type use a list. The taxonomy stays
  closed and auditable — we own the mapping rather than opening the vocabulary.
- **Fail-closed re-identification:** an unresolved `[[TOKEN]]` is never spoken to the payer.
- **Library hardening:** input is sanitized so external text can't masquerade as a token
  (injection); LLM-mangled tokens (`[[ name 1 ]]`, `[NAME-1]`) are repaired back to canonical
  when they resolve in the vault (never invented); and detection is fail-safe — a NER crash
  falls back to regex-only, then to an emergency canary redaction, so a detector bug can't
  leak structured PHI or crash the turn. Token survival through Gemini is empirically
  verified (zero mangling across models in `tests/test_llm_roundtrip.py` + the probe).

## HIPAA Safe Harbor coverage (§3.3)

| # | Identifier | Token |
|---|---|---|
| 1 | Names | `[[NAME_n]]` |
| 2 | Geographic < state | `[[STREET_ADDRESS_n]]`, `[[CITY_n]]`, `[[ZIP_CODE_n]]` (State retained) |
| 3 | Dates / ages > 89 | `[[DATE_n]]` (year retained), `[[AGE_OVER_89_n]]` |
| 4 | Telephone | `[[PHONE_n]]` |
| 5 | Fax | `[[FAX_n]]` |
| 6 | Email | `[[EMAIL_n]]` |
| 7 | SSN | `[[SSN_n]]` |
| 8 | MRN | `[[MRN_n]]` |
| 9 | Health-plan beneficiary | `[[BENEFICIARY_ID_n]]`, `[[MBI_n]]` |
| 10 | Account | `[[ACCOUNT_n]]` |
| 11 | Certificate / license (incl. NPI) | `[[LICENSE_n]]` |
| 12 | Vehicle | `[[VEHICLE_n]]` |
| 13 | Device serial | `[[DEVICE_SERIAL_n]]` (model retained) |
| 14 | Web URL | `[[URL_n]]` |
| 15 | IP address | `[[IP_ADDRESS_n]]` |
| 16–17 | Biometric audio / face photos | **gateway-level** — reject/strip upstream (not a text-codec concern) |
| 18 | Any other unique code | `[[UNIQUE_CODE_n]]` |

Free-text geographic names (`CITY`) lean on the ML NER backend; enable GLiNER
(`PHI_GLINER=1`) for best recall on cities the small spaCy model misses.

## Setup

```bash
uv venv --python 3.12 .venv
uv pip install -e ".[dev,gliner,llm]"
uv run python -m spacy download en_core_web_sm
cp env.example .env        # then add GOOGLE_API_KEY for the live LLM test
```

`.env` (git-ignored) is loaded automatically by the test suite and the UI — no manual
exporting. Keys: `GOOGLE_API_KEY`, optional `GEMINI_MODEL`, optional `PHI_GLINER`.

## Run

```bash
# Tests (offline, fast — GLiNER off)
uv run pytest

# Detection recall + latency on synthetic spoken-form PHI
uv run python -m phi_codec.eval.recall --n 300            # regex+spaCy+GLiNER
uv run python -m phi_codec.eval.recall --n 300 --no-gliner

# Test/demo UI — paste text, try the templates, watch the full round-trip
uv run uvicorn phi_codec.ui.app:app --reload                # GLiNER off (voice-representative)
PHI_GLINER=1 uv run uvicorn phi_codec.ui.app:app --reload   # enable ML name backend (demo/eval only)
# open http://127.0.0.1:8000

# Gated: tokens survive a live Gemini round-trip (public Gemini Developer API)
# Put GOOGLE_API_KEY in .env (see env.example), then:
uv run pytest tests/test_llm_roundtrip.py -v
```

> ⚠️ These commands run **tests, the eval harness, and the demo UI** — `phi_codec` is a
> library, not a runnable voice agent. The cascading STT→LLM→TTS pipeline is a separate
> app that imports `PHICodec`; see Runtime mode below.

## Runtime mode (voice pipeline)

For the real-time cascading pipeline, the recommended configuration is **regex + spaCy
with GLiNER OFF**, plus **known-value seeding**:

```python
codec = PHICodec(CodecConfig(use_gliner=False))   # tokenize p95 ~3ms (vs ~58ms +GLiNER)
await codec.seed_session(call_id, patient_record)  # name/DOB/IDs/address matched at tier-0
```

Why GLiNER-off is the voice default:
- **Tokenize is on the critical path** (STT→LLM, inside the turn budget); ~3ms is invisible, ~58ms is a real slice of a sub-second turn.
- **Seeding covers the patient's free-text PHI deterministically**, so GLiNER's main job (free-text names) is largely already done — more reliably than ML.
- **In-process GLiNER is CPU-bound and doesn't scale** under concurrent calls.

Enable GLiNER **only** if real-transcript eval shows the *unseeded* free-text miss rate
(payer-introduced names/places) is unacceptable — and then prefer a **GPU sidecar** or an
**async secondary check** so it never blocks the turn. The detection timeout already falls
back to regex-only, so a GLiNER spike degrades safely. Note: without seeding, free-text
name recall drops to spaCy-sm levels and GLiNER (or a city gazetteer) becomes more justified.

## Layout

```
phi_codec/
  codec.py              PHICodec async facade: open/tokenize/reidentify/reidentify_args/close
  config.py             entity taxonomy, token syntax, thresholds, TTLs
  detection/            normalizer · engine (Presidio) · recognizers · gliner_backend · leak_canary
  tokens/               token surface syntax · overlap resolution + substitution
  vault/                base · memory_vault · crypto (envelope) · audit (append-only)
  formatting/tts.py     spoken-readback formatting
  eval/                 synth (spoken-form generator) · recall (harness)
  ui/                   FastAPI app + single-file playground
```
