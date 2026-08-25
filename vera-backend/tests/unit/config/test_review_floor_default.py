"""The confidence floor's defaults must not drift apart: the module constant every
`review.py` caller falls back to, the setting the app layer injects, and
`PostCallConsumer`'s own — which already diverges."""

import inspect

from control_plane.post_call_consumer import PostCallConsumer
from vera_core.config.settings import Settings
from vera_core.forms.review import REVIEW_CONFIDENCE_FLOOR


def test_settings_default_matches_the_module_constant() -> None:
    # An import from `config` into `forms` would couple the layers, so the two are pinned
    # here instead. If this fails, one of them moved and every gate silently disagreed.
    assert Settings.model_fields["post_call_review_floor"].default == REVIEW_CONFIDENCE_FLOOR


def test_post_call_consumer_default_is_a_known_divergent_floor() -> None:
    """Pinned at its divergent 60, rather than asserted equal to the other two, so changing
    it stays a deliberate decision — unifying it would be a behavior change."""
    # Production never observes it (`main.py` always passes the setting), but a bare
    # construction — a script, a future call site — would silently get a third floor.
    assert inspect.signature(PostCallConsumer.__init__).parameters["review_floor"].default == 60
