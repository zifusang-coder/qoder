import json
import uuid

import pytest

from app.agent.blackboard import (
    MAX_ENTRY_BYTES,
    append_entry,
    recent_entries,
    render_entries,
)
from app.models.agent import AgentRun
from app.models.base import DEFAULT_TENANT_ID


@pytest.fixture
async def parent(db_session):
    run = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=uuid.uuid4(), goal="目标", mode="team"
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)
    return run


@pytest.mark.asyncio
async def test_seq_increments_and_kind(db_session, parent):
    a = await append_entry(db_session, run=parent, kind="goal", content={"goal": "g"})
    b = await append_entry(db_session, run=parent, kind="delegation", role="scout",
                           content={"task": "t"})
    await db_session.commit()
    assert (a.seq, b.seq) == (1, 2)
    assert a.role == "" and b.role == "scout"


@pytest.mark.asyncio
async def test_scrub_and_truncate(db_session, parent):
    entry = await append_entry(
        db_session, run=parent, kind="result",
        content={"api_key": "sk-secret", "text": "x" * 20000},
    )
    await db_session.commit()
    assert "api_key" not in entry.content
    assert len(json.dumps(entry.content, ensure_ascii=False).encode()) <= MAX_ENTRY_BYTES


@pytest.mark.asyncio
async def test_recent_returns_tail_ascending(db_session, parent):
    for i in range(1, 6):
        await append_entry(db_session, run=parent, kind="result", content={"i": i})
    await db_session.commit()
    recent = await recent_entries(db_session, parent.id, 2)
    assert [e.seq for e in recent] == [4, 5]
    rendered = render_entries(recent)
    assert rendered[0]["seq"] == 4 and rendered[0]["content"] == {"i": 4}