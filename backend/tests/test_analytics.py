import uuid
from datetime import date

from sqlalchemy import func, select

from app.api.analytics import MAX_CSV_BYTES
from app.models.analytics import AnalyticsSnapshot
from app.models.project import Content, Project

OTHER_TENANT = "00000000-0000-0000-0000-000000000002"
HEADER = (
    "content_id,platform,captured_at,views,likes,comments,shares,saves,"
    "followers_delta"
)


def _csv(*rows: str, header: str = HEADER) -> bytes:
    return ("\n".join([header, *rows]) + "\n").encode("utf-8")


def _row(
    content_id,
    *,
    platform: str = "wechat",
    captured_at: str = "2026-10-01",
    views: int = 0,
    likes: int = 0,
    comments: int = 0,
    shares: int = 0,
    saves: int = 0,
    followers_delta: int = 0,
) -> str:
    return (
        f"{content_id},{platform},{captured_at},{views},{likes},{comments},"
        f"{shares},{saves},{followers_delta}"
    )


async def _create_content(client, *, title="数据内容", platform="wechat"):
    project = await client.post(
        "/api/projects", json={"title": f"数据项目-{uuid.uuid4().hex[:6]}"}
    )
    assert project.status_code == 201
    project_id = project.json()["id"]
    content = await client.post(
        f"/api/projects/{project_id}/contents",
        json={"platform": platform, "title": title},
    )
    assert content.status_code == 201
    return project_id, content.json()["id"]


async def _import(client, data: bytes):
    return await client.post(
        "/api/analytics/import",
        files={"file": ("analytics.csv", data, "text/csv")},
    )


async def _snapshot_count(db_session) -> int:
    return (
        await db_session.execute(
            select(func.count()).select_from(AnalyticsSnapshot)
        )
    ).scalar_one()


async def _get_overview(client, project_id):
    response = await client.get(
        "/api/analytics/overview", params={"project_id": project_id}
    )
    assert response.status_code == 200
    return response.json()


async def test_import_and_overview_is_idempotent(client, db_session):
    project_id, content_id = await _create_content(client)
    data = _csv(
        _row(
            content_id,
            captured_at="2026-10-01",
            views=1000,
            likes=100,
            comments=10,
            shares=5,
            saves=5,
            followers_delta=3,
        )
    )

    first = await _import(client, data)
    assert first.status_code == 200
    assert first.json() == {"upserted": 1, "errors": []}

    overview = await _get_overview(client, project_id)
    assert len(overview["items"]) == 1
    item = overview["items"][0]
    assert item["content_id"] == content_id
    assert item["title"] == "数据内容"
    assert item["platform"] == "wechat"
    assert item["has_data"] is True
    assert item["captured_at"] == "2026-10-01"
    assert item["views"] == 1000
    assert item["followers_delta"] == 3
    assert overview["totals"] == {
        "views": 1000,
        "likes": 100,
        "comments": 10,
        "shares": 5,
        "saves": 5,
        "followers_delta": 3,
    }
    assert overview["engagement_rate"] == 0.12

    second = await _import(client, data)
    assert second.status_code == 200
    assert second.json() == {"upserted": 1, "errors": []}
    assert await _snapshot_count(db_session) == 1

    after = await _get_overview(client, project_id)
    assert after["totals"] == overview["totals"]


async def test_import_updates_existing_row(client, db_session):
    project_id, content_id = await _create_content(client)
    await _import(client, _csv(_row(content_id, views=100, likes=10)))
    assert await _snapshot_count(db_session) == 1

    response = await _import(client, _csv(_row(content_id, views=200, likes=20)))
    assert response.status_code == 200
    assert response.json() == {"upserted": 1, "errors": []}
    assert await _snapshot_count(db_session) == 1

    overview = await _get_overview(client, project_id)
    assert overview["items"][0]["views"] == 200
    assert overview["items"][0]["likes"] == 20
    assert overview["totals"]["views"] == 200


async def test_overview_uses_latest_snapshot_per_content(client, db_session):
    project_id, content_id = await _create_content(client)
    await _import(
        client,
        _csv(
            _row(content_id, captured_at="2026-10-01", views=100, likes=1),
            _row(content_id, captured_at="2026-10-03", views=300, likes=3),
        ),
    )
    assert await _snapshot_count(db_session) == 2

    overview = await _get_overview(client, project_id)
    assert len(overview["items"]) == 1
    assert overview["items"][0]["captured_at"] == "2026-10-03"
    assert overview["items"][0]["views"] == 300
    assert overview["totals"]["views"] == 300
    assert overview["totals"]["likes"] == 3


async def test_invalid_rows_are_skipped(client, db_session):
    project_id, content_id = await _create_content(client)
    unknown_id = uuid.uuid4()
    data = _csv(
        _row(unknown_id, captured_at="2026-10-02", views=9),
        "not-a-uuid,wechat,2026-10-03,9,0,0,0,0,0",
        _row(content_id, captured_at="2026/10/04", views=9),
        _row(content_id, captured_at="2026-10-05", views=-1),
        f"{content_id},wechat,2026-10-06,1,abc,0,0,0,0",
        _row(content_id, captured_at="2026-10-07", views=10, likes=1),
    )

    response = await _import(client, data)
    assert response.status_code == 200
    body = response.json()
    assert body["upserted"] == 1
    assert body["errors"] == [
        "第 2 行：内容不存在或不属于当前租户",
        "第 3 行：内容不存在或不属于当前租户",
        "第 4 行：日期格式应为 YYYY-MM-DD",
        "第 5 行：views 必须是非负整数",
        "第 6 行：likes 必须是非负整数",
    ]

    assert await _snapshot_count(db_session) == 1
    snapshot = (
        await db_session.execute(select(AnalyticsSnapshot))
    ).scalar_one()
    assert snapshot.captured_at == date(2026, 10, 7)
    assert snapshot.views == 10
    overview = await _get_overview(client, project_id)
    assert overview["totals"]["views"] == 10


async def test_invalid_csv_inputs(client, db_session):
    missing = await _import(
        client, b"content_id,platform\nabc,wechat\n"
    )
    assert missing.status_code == 422
    assert missing.json()["code"] == "invalid_csv"
    assert "captured_at" in missing.json()["message"]

    broken_utf8 = await _import(
        client, b"content_id,platform,captured_at\n\xff\xfe,wechat,2026-10-01\n"
    )
    assert broken_utf8.status_code == 422
    assert broken_utf8.json()["code"] == "invalid_csv"

    empty = await _import(client, b"")
    assert empty.status_code == 422
    assert empty.json()["code"] == "invalid_csv"

    header_only = await _import(client, (HEADER + "\n").encode())
    assert header_only.status_code == 200
    assert header_only.json() == {"upserted": 0, "errors": []}
    assert await _snapshot_count(db_session) == 0


async def test_rejects_large_file(client, db_session):
    padding = "x" * (MAX_CSV_BYTES + 1)
    data = (
        f"content_id,platform,captured_at,pad\n"
        f"{uuid.uuid4()},wechat,2026-10-01,{padding}\n"
    ).encode()

    response = await _import(client, data)
    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"
    assert await _snapshot_count(db_session) == 0


async def test_tenant_isolation(client, db_session):
    other_project_id = uuid.uuid4()
    other_content_id = uuid.uuid4()
    db_session.add(
        Project(id=other_project_id, tenant_id=OTHER_TENANT, title="其他租户项目")
    )
    await db_session.flush()
    db_session.add(
        Content(
            id=other_content_id,
            tenant_id=OTHER_TENANT,
            project_id=other_project_id,
            platform="wechat",
        )
    )
    await db_session.commit()

    response = await _import(
        client, _csv(_row(other_content_id, captured_at="2026-10-01", views=5))
    )
    assert response.status_code == 200
    body = response.json()
    assert body["upserted"] == 0
    assert body["errors"] == ["第 2 行：内容不存在或不属于当前租户"]
    assert await _snapshot_count(db_session) == 0

    overview = await client.get(
        "/api/analytics/overview", params={"project_id": other_project_id}
    )
    assert overview.status_code == 404
    assert overview.json()["code"] == "not_found"


async def test_overview_without_snapshot(client, db_session):
    project_id, with_data_id = await _create_content(client, title="有数据")
    empty = await client.post(
        f"/api/projects/{project_id}/contents",
        json={"platform": "zhihu", "title": "无数据"},
    )
    assert empty.status_code == 201
    empty_id = empty.json()["id"]

    await _import(
        client,
        _csv(
            _row(
                with_data_id,
                captured_at="2026-10-01",
                views=50,
                likes=5,
                saves=1,
            )
        ),
    )

    overview = await _get_overview(client, project_id)
    items = {item["content_id"]: item for item in overview["items"]}
    assert len(items) == 2

    filled = items[with_data_id]
    assert filled["has_data"] is True
    assert filled["views"] == 50

    blank = items[empty_id]
    assert blank["has_data"] is False
    assert blank["captured_at"] is None
    assert blank["platform"] == "zhihu"
    assert blank["views"] == 0
    assert blank["likes"] == 0
    assert blank["followers_delta"] == 0

    assert overview["totals"]["views"] == 50
    assert overview["totals"]["likes"] == 5
    assert overview["totals"]["saves"] == 1
    assert overview["engagement_rate"] == 0.12


async def test_overview_project_lookup(client, db_session):
    missing = await client.get(
        "/api/analytics/overview", params={"project_id": uuid.uuid4()}
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"

    malformed = await client.get(
        "/api/analytics/overview", params={"project_id": "not-a-uuid"}
    )
    assert malformed.status_code == 422
    assert malformed.json()["code"] == "validation_error"


async def test_bom_extra_columns_and_duplicate_rows(client, db_session):
    project_id, content_id = await _create_content(client)
    header = HEADER + ",note"
    first_row = f"{content_id},,2026-10-01,10,1,0,0,0,0,第一条"
    second_row = f"{content_id},wechat,2026-10-01,20,2,0,0,0,0,第二条"
    data = ("\ufeff" + header + "\n" + first_row + "\n" + second_row + "\n").encode(
        "utf-8"
    )

    response = await _import(client, data)
    assert response.status_code == 200
    assert response.json() == {"upserted": 2, "errors": []}
    assert await _snapshot_count(db_session) == 1

    snapshot = (
        await db_session.execute(select(AnalyticsSnapshot))
    ).scalar_one()
    assert snapshot.platform == "wechat"
    assert snapshot.views == 20
    assert snapshot.raw["note"] == "第二条"

    overview = await _get_overview(client, project_id)
    assert overview["items"][0]["views"] == 20
