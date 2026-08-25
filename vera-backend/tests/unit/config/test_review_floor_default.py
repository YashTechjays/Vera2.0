"""The confidence floor's defaults must not drift apart: the module constant every
`review.py` caller falls back to, the setting the app layer injects, and
`PostCallConsumer`'s own default must all agree."""

import inspect

from control_plane.post_call_consumer import PostCallConsumer
from vera_core.config.settings import Settings
from vera_core.forms.review import REVIEW_CONFIDENCE_FLOOR


def test_settings_default_matches_the_module_constant() -> None:
    # An import from `config` into `forms` would couple the layers, so the two are pinned
    # here instead. If this fails, one of them moved and every gate silently disagreed.
    assert Settings.model_fields["post_call_review_floor"].default == REVIEW_CONFIDENCE_FLOOR


def test_post_call_consumer_default_matches_the_module_constant() -> None:
    # Production never observes this default (`main.py` always passes the setting
    # explicitly), but a bare construction — a script, a future call site — must not
    # silently pick up a third floor.
    default = inspect.signature(PostCallConsumer.__init__).parameters["review_floor"].default
    assert default == REVIEW_CONFIDENCE_FLOOR
