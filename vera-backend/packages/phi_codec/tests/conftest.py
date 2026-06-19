"""Shared fixtures. The codec is expensive to build (loads spaCy), so it's
session-scoped and reused. GLiNER is off in tests for speed/determinism; the
regex+spaCy path is what these tests assert on.
"""

import pytest
from dotenv import load_dotenv

# Loaded before test modules are collected, so the GOOGLE_API_KEY gate in
# test_llm_roundtrip.py picks up a project-root .env without manual exporting.
load_dotenv()

from phi_codec.codec import PHICodec  # noqa: E402
from phi_codec.config import CodecConfig  # noqa: E402


@pytest.fixture(scope="session")
def codec() -> PHICodec:
    return PHICodec(CodecConfig(use_gliner=False))
