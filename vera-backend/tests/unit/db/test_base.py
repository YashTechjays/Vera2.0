from uuid import UUID

from sqlalchemy.orm import DeclarativeBase

from vera_core.db.base import NAMING_CONVENTION, TenantScopedMixin, uuid7


class _Base(DeclarativeBase):
    pass


class _Widget(TenantScopedMixin, _Base):
    __tablename__ = "widget"


def test_uuid7_is_stdlib_uuid_version_7() -> None:
    value = uuid7()
    assert type(value) is UUID
    assert value.version == 7


def test_uuid7_is_time_ordered() -> None:
    ids = [uuid7() for _ in range(100)]
    assert ids == sorted(ids)


def test_tenant_scoped_mixin_shape() -> None:
    cols = _Widget.__table__.columns
    assert set(cols.keys()) == {"id", "tenant_id", "created_at", "updated_at"}
    assert cols["id"].primary_key
    assert str(cols["id"].type) == "UUID"
    id_default = cols["id"].default
    assert id_default is not None and id_default.is_callable  # client-side uuid7
    assert not cols["tenant_id"].nullable
    assert cols["tenant_id"].index
    assert cols["created_at"].server_default is not None
    assert cols["updated_at"].server_default is not None
    assert cols["updated_at"].onupdate is not None


def test_naming_convention_covers_all_constraint_kinds() -> None:
    assert set(NAMING_CONVENTION) == {"ix", "uq", "ck", "fk", "pk"}
