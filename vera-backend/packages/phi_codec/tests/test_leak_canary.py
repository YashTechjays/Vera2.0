"""Leak canary unit tests — it must catch residual PHI shapes but not flag tokens."""

from phi_codec.detection.leak_canary import scan


def test_clean_tokenized_text_passes():
    assert scan("member [[MEMBER_ID_1]] for [[NAME_1]]").ok


def test_tokens_do_not_trip_long_digit_run():
    # [[MEMBER_ID_12]] contains digits but must be masked out before scanning.
    assert scan("here is [[MEMBER_ID_12]] and [[SSN_3]]").ok


def test_residual_ssn_is_flagged():
    res = scan("the ssn is 521-23-8765 still in the clear")
    assert not res.ok
    assert any(f.kind == "ssn_like" for f in res.findings)


def test_residual_long_digit_run_is_flagged():
    res = scan("untokenized 9876543210 leaked")
    assert not res.ok
    assert any(f.kind in {"long_digit_run", "phone_like"} for f in res.findings)


def test_residual_email_is_flagged():
    res = scan("contact jane.doe@example.com please")
    assert not res.ok
    assert any(f.kind == "email_like" for f in res.findings)
