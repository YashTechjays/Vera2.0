"""The confidence floor has two defaults that must not diverge: the module constant every
`review.py` caller falls back to, and the setting the app layer injects."""

from vera_core.config.settings import Settings
from vera_core.forms.review import REVIEW_CONFIDENCE_FLOOR


def test_settings_default_matches_the_module_constant() -> None:
    # An import from `config` into `forms` would couple the layers, so the two are pinned
    # here instead. If this fails, one of them moved and every gate silently disagreed.
    assert Settings.model_fields["post_call_review_floor"].default == REVIEW_CONFIDENCE_FLOOR
