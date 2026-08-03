"""Synthesize one phrase across Cartesia model ids and phrasings, and write the mp3s to compare.

Diagnostic only — nothing imports this. Use synthetic strings, never real PHI.

Usage (the --with certifi gives a bare framework python3 a CA bundle):
    uv run --no-project --with certifi python scripts/tts_probe.py
    uv run --no-project --with certifi python scripts/tts_probe.py --set comma
    uv run --no-project --with certifi python scripts/tts_probe.py "Y as in yellow" --out /tmp/probe
"""

import argparse
import functools
import importlib
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.cartesia.ai/tts/bytes"

# Mirrors apps/agent_worker/src/agent_worker/cascade.py + the livekit cartesia plugin defaults,
# so a difference here is a difference on a real call.
API_VERSION = "2025-04-16"
VOICE_ID = "f786b574-daa5-4673-aa0c-cbe3e8534c02"  # Katie - Friendly Fixer
LANGUAGE = "en"
EMOTION = "confident"

DEFAULT_TEXT = "YA123456789"
DEFAULT_MODELS = [
    "sonic-3.5",  # floating alias — regression watch; production runs the pin below
    "sonic-3.5-2026-05-04",  # pinned snapshot
    "sonic-latest",  # beta channel
    "sonic-3",  # previous generation, as a control
]

# Transcript phrasings of the same input. `comma` is the shipped workaround
# (agent_worker.cartesia_workaround): a leading comma is what stops sonic-3.5 misreading the
# first character of an utterance-initial <spell> tag.
VARIANT_SETS = {
    "default": {
        "plain": "{text}",
        "spell": "<spell>{text}</spell>",
    },
    "comma": {
        "1-bare": "<spell>{text}</spell>",
        "2-lead-in": ", <spell>{text}</spell>",
        "3-lead-in-short-tail": ", <spell>{head}1234</spell>",
        "4-lead-in-digits-only": ", <spell>1234567</spell>",
        "5-mid-sentence": "The member ID is <spell>{text}</spell>.",
    },
    # Synthesize the input untouched — feed it what _tts_spoken_text actually emits, so the
    # audio you sign off on is the string the worker sends, not a hand-typed approximation.
    "verbatim": {"as-sent": "{text}"},
}


def resolve_api_key() -> str:
    key = os.environ.get("CARTESIA_API_KEY")
    if key:
        return key

    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.is_file():
        match = re.search(r"^\s*CARTESIA_API_KEY\s*=\s*(.+)$", env_file.read_text(), re.MULTILINE)
        if match:
            return match.group(1).strip().strip("'\"")

    sys.exit("CARTESIA_API_KEY is not set (env or vera-backend/.env)")


@functools.cache
def ssl_context() -> ssl.SSLContext:
    # A bare framework python3 has no CA bundle; borrow certifi's when it is importable.
    try:
        certifi = importlib.import_module("certifi")
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def synthesize(api_key: str, model: str, transcript: str) -> bytes:
    payload: dict[str, object] = {
        "model_id": model,
        "transcript": transcript,
        "voice": {"mode": "id", "id": VOICE_ID},
        "output_format": {"container": "mp3", "sample_rate": 44100, "bit_rate": 128000},
        "language": LANGUAGE,
    }
    if model.startswith("sonic-3"):
        payload["generation_config"] = {"emotion": EMOTION}

    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Cartesia-Version": API_VERSION,
            "X-API-Key": api_key,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=60, context=ssl_context()) as response:
        return response.read()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", nargs="?", default=DEFAULT_TEXT)
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--set", default="default", choices=sorted(VARIANT_SETS))
    parser.add_argument("--out", default="tts-probe", type=Path)
    args = parser.parse_args()

    api_key = resolve_api_key()
    args.out.mkdir(parents=True, exist_ok=True)
    fields = {"text": args.text, "head": args.text[:1]}

    print(f'text: "{args.text}"  ->  {args.out}/\n')
    manifest = []
    failures = 0
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        for label, template in VARIANT_SETS[args.set].items():
            transcript = template.format(**fields)
            path = args.out / f"{model}__{label}.mp3"
            try:
                audio = synthesize(api_key, model, transcript)
            except urllib.error.HTTPError as exc:
                failures += 1
                print(f"  FAIL {path.name}  HTTP {exc.code}: {exc.read().decode(errors='replace')}")
                continue
            except urllib.error.URLError as exc:
                failures += 1
                print(f"  FAIL {path.name}  {exc.reason}")
                continue
            path.write_bytes(audio)
            manifest.append(f"{path.name}\n    {transcript}")
            print(f"  ok   {path.name}  {transcript}")

    (args.out / "manifest.txt").write_text("\n".join(manifest) + "\n")
    print(f"\nopen {args.out}" if not failures else f"\n{failures} request(s) failed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
