"""FastAPI test/demo harness for the PHI codec.

Thin async wrapper over the SAME ``PHICodec`` the voice pipeline uses, so the UI
exercises the real code path. One shared codec (model load is slow); sessions are
created on demand. GLiNER is off by default for fast startup — set PHI_GLINER=1 to
enable the ML name/location backend.

Run:  uv run uvicorn phi_codec.ui.app:app --reload
Open: http://127.0.0.1:8000
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

load_dotenv()  # PHI_GLINER, etc. from a project-root .env

from ..codec import PHICodec
from ..config import CodecConfig
from ..detection.normalizer import normalize
from ..eval.recall import run as run_recall

_USE_GLINER = os.getenv("PHI_GLINER", "0") == "1"
_STATIC = Path(__file__).parent / "static"

app = FastAPI(title="PHI Codec Playground")
codec = PHICodec(CodecConfig(use_gliner=_USE_GLINER))


# --------------------------------------------------------------------- schemas
class SessionReq(BaseModel):
    session_id: str = "demo"


class DetectReq(BaseModel):
    session_id: str = "demo"
    text: str
    spoken: bool = True  # informational; normalization always runs in tokenize


class TokenizeReq(BaseModel):
    session_id: str = "demo"
    text: str
    turn_id: str = "t0"


class ReidReq(BaseModel):
    session_id: str = "demo"
    text: str


class ArgsReq(BaseModel):
    session_id: str = "demo"
    args: dict


class SeedReq(BaseModel):
    session_id: str = "demo"
    known: dict  # {"NAME": "John Smith", "BENEFICIARY_ID": "XYZ987654321", ...}


class EvalReq(BaseModel):
    n: int = 150


# --------------------------------------------------------------------- routes
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


@app.get("/config")
async def config() -> dict:
    return {"use_gliner": _USE_GLINER, "gliner_model": codec.config.gliner_model,
            "active_entities": [e.value for e in codec.config.active_entities],
            "vault_scheme": getattr(codec.vault, "encryptor", None) and codec.vault.encryptor.scheme}


@app.post("/session/open")
async def session_open(req: SessionReq) -> dict:
    await codec.open_session(req.session_id)
    return {"session_id": req.session_id, "status": "open"}


@app.post("/session/close")
async def session_close(req: SessionReq) -> dict:
    await codec.close_session(req.session_id)
    return {"session_id": req.session_id, "status": "closed"}


@app.post("/session/seed")
async def session_seed(req: SeedReq) -> dict:
    try:
        seeded = await codec.seed_session(req.session_id, req.known)
    except ValueError as exc:
        return {"error": str(exc), "valid_types": [e.value for e in codec.config.active_entities]}
    return {"session_id": req.session_id, "seeded": seeded, "count": len(seeded)}


@app.get("/session/{session_id}/vault")
async def session_vault(session_id: str, reveal: bool = False) -> dict:
    await codec.open_session(session_id)
    entries = await codec.vault.dump(session_id)
    return {
        "session_id": session_id,
        "entries": [
            {
                "token": e.token,
                "entity_type": e.entity_type,
                "raw": e.raw_value if reveal else _mask(e.raw_value),
                "recognizer": e.recognizer,
                "score": round(e.score, 3),
                "first_turn": e.first_turn_id,
            }
            for e in entries
        ],
    }


@app.post("/detect")
async def detect(req: DetectReq) -> dict:
    await codec.open_session(req.session_id)
    normalized = normalize(req.text)
    import asyncio

    detections = await asyncio.to_thread(codec.engine.detect, normalized)
    from ..tokens.tokenizer import resolve_overlaps

    kept = resolve_overlaps(detections)
    return {
        "original": req.text,
        "normalized": normalized,
        "entities": [
            {"entity_type": d.entity_type.value, "text": d.text, "start": d.start,
             "end": d.end, "score": round(d.score, 3), "recognizer": d.recognizer}
            for d in kept
        ],
    }


@app.post("/tokenize")
async def tokenize(req: TokenizeReq) -> dict:
    await codec.open_session(req.session_id)
    res = await codec.tokenize(req.session_id, req.text, turn_id=req.turn_id)
    return {
        "normalized": res.normalized_text,
        "text_tokenized": res.text_tokenized,
        "leak_ok": res.leak_ok,
        "leak_findings": [{"kind": f.kind, "text": f.text} for f in res.leak_findings],
        "degraded": res.degraded,
        "latency_ms": round(res.latency_ms, 2),
        "entities": [
            {"entity_type": e.entity_type, "raw": _mask(e.raw_text), "token": e.token,
             "score": round(e.score, 3), "recognizer": e.recognizer}
            for e in res.entities
        ],
    }


@app.post("/reidentify")
async def reidentify(req: ReidReq) -> dict:
    await codec.open_session(req.session_id)
    res = await codec.reidentify(req.session_id, req.text)
    return {"text": res.text, "ok": res.ok, "unresolved": res.unresolved,
            "latency_ms": round(res.latency_ms, 2)}


@app.post("/reidentify_args")
async def reidentify_args(req: ArgsReq) -> dict:
    await codec.open_session(req.session_id)
    return {"resolved": await codec.reidentify_args(req.session_id, req.args)}


@app.post("/eval/recall")
async def eval_recall(req: EvalReq) -> dict:
    report = await run_recall(req.n, seed=0, use_gliner=_USE_GLINER)
    return {
        "n": report["n"],
        "latency_p50": round(report["latency_p50"], 2),
        "latency_p95": round(report["latency_p95"], 2),
        "leak_turns": report["leak_turns"],
        "by_type": {
            etype: {"n": st.total,
                    "redaction_recall": round(st.redaction_recall, 4),
                    "type_recall": round(st.type_recall, 4)}
            for etype, st in report["stats"].items()
        },
    }


def _mask(value: str) -> str:
    if len(value) <= 2:
        return "•" * len(value)
    return value[0] + "•" * (len(value) - 2) + value[-1]
