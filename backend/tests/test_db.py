from sqlalchemy import String, select, text
from sqlalchemy.orm import Mapped, mapped_column

from app.config import DEFAULT_TENANT_ID
from app.models.base import TenantScoped


class _Thing(TenantScoped):
    __tablename__ = "test_things"

    label: Mapped[str | None] = mapped_column(String(32), nullable=True)


async def test_tenant_default_and_session(db_session):
    db_session.add(_Thing())
    await db_session.commit()
    row = (await db_session.execute(select(_Thing))).scalar_one()
    assert row.tenant_id == DEFAULT_TENANT_ID
    assert row.id is not None


async def test_updated_at_available_after_update(db_session):
    thing = _Thing()
    db_session.add(thing)
    await db_session.commit()
    thing.label = "changed"
    await db_session.commit()
    assert thing.updated_at is not None


async def test_sqlite_foreign_keys_enabled(db_session):
    value = (await db_session.execute(text("PRAGMA foreign_keys"))).scalar_one()
    assert value == 1
