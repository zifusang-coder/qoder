from sqlalchemy import select

from app.models.base import DEFAULT_TENANT_ID, TenantScoped


class _Thing(TenantScoped):
    __tablename__ = "test_things"


async def test_tenant_default_and_session(db_session):
    db_session.add(_Thing())
    await db_session.commit()
    row = (await db_session.execute(select(_Thing))).scalar_one()
    assert row.tenant_id == DEFAULT_TENANT_ID
    assert row.id is not None
