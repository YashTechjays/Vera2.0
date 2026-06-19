"""phi_codec — bidirectional PHI de-identification / re-identification codec.

The codec tokenizes raw STT text before it reaches the LLM and re-identifies
LLM output before it reaches TTS or the payer-API connector. See codec.PHICodec.
"""

from .config import CodecConfig, EntityType
from .tokens.token import PHIToken, TOKEN_RE

__all__ = ["CodecConfig", "EntityType", "PHIToken", "TOKEN_RE"]
