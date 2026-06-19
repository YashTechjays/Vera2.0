# PHI Protection in the Voice AI Pipeline

How `phi_codec` keeps raw PHI out of the LLM (and logs/transcripts) while still letting the
agent speak real values to the caller and send exact values to the payer API.

```mermaid
flowchart TB
    subgraph CALL["📞 Eligibility / Prior-Auth Call (per session)"]
        direction TB

        SEED["🌱 seed_session()<br/>Pre-load known patient PHI<br/>name · DOB · member ID · SSN · address<br/>(EHR field aliases → canonical types)"]

        STT["🎙️ Deepgram STT<br/>raw spoken transcript<br/><i>'...member X Y Z nine eight seven...'</i>"]

        subgraph TOK["🔒 codec.tokenize()  —  RAW ➜ TOKENS  (before LLM)"]
            direction TB
            T1["1 Sanitize input<br/>(neutralize injected [[..]] delimiters)"]
            T2["2 Normalize spoken form<br/>'X Y Z nine eight seven' → 'XYZ987'"]
            T3["3 Detect PHI (fail-safe, recall-first)"]
            T4["4 Resolve overlaps<br/>(score → type specificity)"]
            T5["5 Mint/reuse vault tokens<br/>[[NAME_1]] [[BENEFICIARY_ID_1]]"]
            T6["6 Leak canary<br/>scan output for residual PHI shapes"]
            T1 --> T2 --> T3 --> T4 --> T5 --> T6
        end

        subgraph DET["🔎 Detection — 3 tiers (+ fallbacks)"]
            direction TB
            D0["Tier 0 · Known-value seeding<br/>exact + phonetic (Metaphone/Jaro-Winkler)<br/><b>score 1.0, wins all</b>"]
            D1["Tier 1 · Regex recognizers<br/>SSN · member ID · phone · MBI · NPI · ZIP…<br/>context-aware, ~2-3ms"]
            D2["Tier 2 · GLiNER ML NER<br/>free-text names/cities<br/><i>OFF by default for voice</i>"]
            FB["⛑️ Fallbacks<br/>NER timeout → regex-only<br/>regex fails → canary redaction<br/>(never 'no detection')"]
            D0 --> D1 --> D2 --> FB
        end

        LLM["🤖 Gemini LLM<br/><b>sees ONLY tokens — never raw PHI</b><br/>'...patient [[NAME_1]] member [[BENEFICIARY_ID_1]]...'<br/>instructed to never alter [[..]]"]

        subgraph REID["🔓 RE-IDENTIFY  —  TOKENS ➜ RAW  (two exits)"]
            direction TB
            R1["codec.reidentify()  →  TTS path<br/>repair mangled tokens → vault lookup<br/>format for speech (spell-out IDs):<br/>'X Y Z 9 8 7…'"]
            R2["codec.reidentify_args()  →  payer API path<br/>vault lookup, <b>exact values</b> (no TTS format)<br/>{member_id: 'XYZ987'}"]
        end

        TTS["🔊 Cartesia TTS<br/>speaks real values to caller"]
        API["🏥 Payer API<br/>receives exact raw values"]

        SEED -.->|pre-mint tokens| VAULT
        STT --> TOK
        T3 <-->|detect| DET
        TOK -->|tokenized text| LLM
        LLM -->|response w/ tokens| R1
        LLM -->|tool-call args w/ tokens| R2
        R1 --> TTS
        R2 --> API
    end

    subgraph SECURE["🛡️ Per-session Vault + Audit (PHI never leaves here in cleartext)"]
        direction TB
        VAULT["🗄️ PHIVault<br/>bidirectional raw ↔ token map<br/>deterministic · lossless · dedup<br/>raw encrypted at rest (Fernet → KMS)"]
        AUDIT["📋 Append-only audit log<br/>token · type · recognizer · score<br/>raw stored ciphertext-only"]
        VAULT --- AUDIT
    end

    T5 <-->|get_or_create_token| VAULT
    R1 <-->|resolve token| VAULT
    R2 <-->|resolve token| VAULT
    TOK -.->|log| AUDIT
    REID -.->|log| AUDIT

    classDef raw fill:#ffe0e0,stroke:#cc0000,color:#000
    classDef safe fill:#e0ffe0,stroke:#008800,color:#000
    classDef codec fill:#e0e8ff,stroke:#0044cc,color:#000
    classDef vault fill:#fff4d0,stroke:#cc8800,color:#000

    class STT,TTS,API,SEED raw
    class LLM safe
    class TOK,REID,DET codec
    class VAULT,AUDIT vault
```

## The one-line idea

**Raw PHI is replaced with `[[TYPE_N]]` tokens *before* the LLM, and restored *after* — so the
model, prompt logs, and transcripts only ever contain tokens.** The real values live encrypted in a
per-session vault and are re-inserted at the very last moment: spelled-out for the TTS voice, or exact
for the payer API.

| Stage | Direction | What crosses the boundary |
|-------|-----------|---------------------------|
| `tokenize()`        | raw → tokens | STT text de-identified before Gemini |
| `reidentify()`      | tokens → raw | LLM reply restored + spell-out formatted for TTS |
| `reidentify_args()` | tokens → raw | LLM tool args restored to exact values for payer API |

**Backstops:** recall-first detection (low score floor) + leak canary on the output + fail-closed
re-identify (unknown tokens are never spoken/sent) + detection fallbacks that never degrade to
"no detection."
