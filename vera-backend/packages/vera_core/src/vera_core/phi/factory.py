"""Factory for the PHI boundary (mirrors config.kms.build_kms).

Returns PassthroughPHIBoundary today. When tokenization lands, this is where the
real PHIBoundary + the `phi_tokenizer_disabled` flag + the prod hard-fail guard
will be selected — the only place that branches on config.
"""

from typing import TYPE_CHECKING

from vera_core.phi.protocol import PassthroughPHIBoundary, PHIBoundaryProtocol

if TYPE_CHECKING:
    from vera_core.config.settings import Settings


def build_phi_boundary(settings: "Settings") -> PHIBoundaryProtocol:
    # TODO(vera-2.x): when the codec is wired, return the real PHIBoundary unless
    # phi_tokenizer_disabled is set; hard-fail if disabled in prod.
    return PassthroughPHIBoundary()
