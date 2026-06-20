from vera_core.config.settings import Settings
from vera_core.phi import PassthroughPHIBoundary, build_phi_boundary


def test_build_phi_boundary_returns_passthrough_for_now() -> None:
    settings = Settings(_env_file=None)
    boundary = build_phi_boundary(settings)
    assert isinstance(boundary, PassthroughPHIBoundary)
