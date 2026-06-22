from vera_core.models import AuthAuditLog


def test_auth_audit_log_seq_is_db_populated() -> None:
    col = AuthAuditLog.__table__.c.seq
    assert col is not None
    assert not col.nullable
    # DB-populated (by the chain trigger), so it is omitted from every INSERT.
    assert col.server_default is not None


def test_hash_columns_present() -> None:
    cols = AuthAuditLog.__table__.c
    assert "prev_hash" in cols
    assert "row_hash" in cols
