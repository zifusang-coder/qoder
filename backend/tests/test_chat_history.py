import uuid
from pathlib import Path

from sqlalchemy import select

from app.api import chat as chat_module
from app.main import app
from app.models.base import DEFAULT_TENANT_ID
from app.models.chat import ChatConversation, ChatMessage
from app.models.profile import Profile
from app.skills.registry import SkillRegistry, get_registry

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"


class RecordingGateway:
    def __init__(self, reply: str = "这是运营建议"):
        self.reply = reply
        self.calls: list[list[dict]] = []

    async def complete(self, messages, **kwargs):
        self.calls.append(messages)
        return self.reply


def _use_gateway(monkeypatch, gateway):
    async def _load(_session, _tenant_id):
        return gateway

    monkeypatch.setattr(chat_module, "load_gateway_default", _load)


async def _messages(db_session, conversation_id):
    rows = (
        await db_session.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.seq)
        )
    ).scalars()
    return list(rows)


async def test_chat_creates_conversation_and_persists_messages(
    client, monkeypatch, db_session
):
    gateway = RecordingGateway()
    _use_gateway(monkeypatch, gateway)

    resp = await client.post("/api/chat", json={"message": "你好，今天适合发什么"})
    assert resp.status_code == 200
    conversation_id = resp.json()["conversation_id"]
    assert conversation_id

    db_session.expire_all()
    conversations = (await db_session.execute(select(ChatConversation))).scalars().all()
    assert len(conversations) == 1
    conversation = conversations[0]
    assert str(conversation.id) == conversation_id
    assert conversation.tenant_id == DEFAULT_TENANT_ID
    assert conversation.title == "你好，今天适合发什么"

    messages = await _messages(db_session, conversation.id)
    assert [(m.role, m.content) for m in messages] == [
        ("user", "你好，今天适合发什么"),
        ("assistant", "这是运营建议"),
    ]
    assert messages[1].intent == "chat"
    assert messages[1].tenant_id == DEFAULT_TENANT_ID


async def test_chat_continues_conversation_with_history(
    client, monkeypatch, db_session
):
    gateway = RecordingGateway()
    _use_gateway(monkeypatch, gateway)

    first = await client.post("/api/chat", json={"message": "你好"})
    conversation_id = first.json()["conversation_id"]

    gateway.calls.clear()
    second = await client.post(
        "/api/chat",
        json={"message": "在吗", "conversation_id": conversation_id},
    )
    assert second.status_code == 200
    assert second.json()["conversation_id"] == conversation_id

    sent = gateway.calls[0]
    assert sent[0]["role"] == "system"
    assert {"role": "user", "content": "你好"} in sent
    assert {"role": "assistant", "content": "这是运营建议"} in sent
    assert sent[-1] == {"role": "user", "content": "在吗"}

    db_session.expire_all()
    conversations = (await db_session.execute(select(ChatConversation))).scalars().all()
    assert len(conversations) == 1
    messages = await _messages(db_session, uuid.UUID(conversation_id))
    assert [m.seq for m in messages] == [1, 2, 3, 4]


async def test_chat_history_is_limited_to_last_ten_messages(
    client, monkeypatch, db_session
):
    gateway = RecordingGateway()
    _use_gateway(monkeypatch, gateway)

    conversation = ChatConversation(
        tenant_id=DEFAULT_TENANT_ID, user_id=uuid.uuid4(), title="旧对话"
    )
    db_session.add(conversation)
    await db_session.flush()
    for seq in range(1, 13):
        db_session.add(
            ChatMessage(
                tenant_id=DEFAULT_TENANT_ID,
                conversation_id=conversation.id,
                seq=seq,
                role="user" if seq % 2 else "assistant",
                content=f"历史 {seq}",
            )
        )
    await db_session.commit()

    resp = await client.post(
        "/api/chat",
        json={"message": "在吗"},
    )
    assert resp.status_code == 200
    # 没有带 conversation_id，应该新建对话而不是续接别人的历史
    assert len(gateway.calls[0]) == 2

    gateway.calls.clear()
    own = await client.post("/api/chat", json={"message": "你好"})
    own_id = own.json()["conversation_id"]
    conversation = (
        await db_session.execute(
            select(ChatConversation).where(ChatConversation.id == uuid.UUID(own_id))
        )
    ).scalar_one()
    for seq in range(3, 15):
        db_session.add(
            ChatMessage(
                tenant_id=DEFAULT_TENANT_ID,
                conversation_id=conversation.id,
                seq=seq,
                role="user",
                content=f"补充 {seq}",
            )
        )
    await db_session.commit()

    gateway.calls.clear()
    resp = await client.post(
        "/api/chat", json={"message": "在吗", "conversation_id": own_id}
    )
    assert resp.status_code == 200
    sent = gateway.calls[0]
    assert len(sent) == 1 + 10 + 1
    assert sent[-1] == {"role": "user", "content": "在吗"}
    assert sent[1]["content"] == "补充 5"


async def test_chat_rejects_conversations_of_other_users(
    client, monkeypatch, db_session
):
    gateway = RecordingGateway()
    _use_gateway(monkeypatch, gateway)

    foreign = ChatConversation(
        tenant_id=DEFAULT_TENANT_ID, user_id=uuid.uuid4(), title="别人的对话"
    )
    other_tenant = ChatConversation(
        tenant_id=str(uuid.uuid4()), user_id=uuid.uuid4(), title="别的租户"
    )
    db_session.add_all([foreign, other_tenant])
    await db_session.commit()

    for conversation in (foreign, other_tenant):
        post = await client.post(
            "/api/chat",
            json={"message": "你好", "conversation_id": str(conversation.id)},
        )
        assert post.status_code == 404
        assert post.json()["message"] == "对话不存在"

        detail = await client.get(f"/api/chat/conversations/{conversation.id}")
        assert detail.status_code == 404

        deleted = await client.delete(f"/api/chat/conversations/{conversation.id}")
        assert deleted.status_code == 404

    assert gateway.calls == []


async def test_conversation_list_detail_and_delete(
    client, monkeypatch, db_session
):
    gateway = RecordingGateway()
    _use_gateway(monkeypatch, gateway)

    first = await client.post("/api/chat", json={"message": "你好"})
    first_id = first.json()["conversation_id"]
    second = await client.post("/api/chat", json={"message": "在吗"})
    second_id = second.json()["conversation_id"]
    assert first_id != second_id

    listing = await client.get("/api/chat/conversations")
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert [item["id"] for item in items] == [second_id, first_id]
    assert items[0]["title"] == "在吗"

    detail = await client.get(f"/api/chat/conversations/{first_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["id"] == first_id
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]

    deleted = await client.delete(f"/api/chat/conversations/{first_id}")
    assert deleted.status_code == 204

    db_session.expire_all()
    assert (
        await db_session.execute(
            select(ChatMessage).where(
                ChatMessage.conversation_id == uuid.UUID(first_id)
            )
        )
    ).scalars().all() == []
    gone = await client.get(f"/api/chat/conversations/{first_id}")
    assert gone.status_code == 404


async def test_skill_turn_is_persisted_with_metadata(
    client, monkeypatch, db_session
):
    _use_gateway(monkeypatch, RecordingGateway("这是运营建议"))
    app.dependency_overrides[get_registry] = lambda: SkillRegistry(SKILLS_DIR)
    try:
        resp = await client.post(
            "/api/chat", json={"message": "写个小红书笔记：通勤穿搭"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["skill_id"] == "xhs-note"

        db_session.expire_all()
        messages = await _messages(db_session, uuid.UUID(body["conversation_id"]))
        assert [m.role for m in messages] == ["user", "assistant"]
        assistant = messages[1]
        assert assistant.intent == "skill"
        assert assistant.skill_id == "xhs-note"
        assert assistant.run_id == body["run_id"]
    finally:
        app.dependency_overrides.pop(get_registry, None)


async def test_conversation_remembers_profile(client, monkeypatch, db_session):
    gateway = RecordingGateway()
    async def _load(_session, _tenant_id):
        return gateway

    monkeypatch.setattr(chat_module, "load_gateway_default", _load)
    profile = Profile(
        id=uuid.uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        name="主账号",
        account_name="效率笔记",
    )
    db_session.add(profile)
    await db_session.commit()

    first = await client.post(
        "/api/chat", json={"message": "你好", "profile_id": str(profile.id)}
    )
    assert first.status_code == 200
    conversation_id = first.json()["conversation_id"]

    await client.post(
        "/api/chat", json={"message": "在吗", "conversation_id": conversation_id}
    )
    assert "<profile_context>" in gateway.calls[-1][0]["content"]
    assert "效率笔记" in gateway.calls[-1][0]["content"]
