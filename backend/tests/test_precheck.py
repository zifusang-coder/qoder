import uuid

from sqlalchemy import select

from app.models.base import DEFAULT_TENANT_ID
from app.models.profile import Profile
from app.models.project import Content, ContentVersion, Project
from app.models.publish import PublishEvent, PublishJob
from app.publish.rules import MAX_LENGTH
from app.services.precheck import run_precheck

OTHER_TENANT = "00000000-0000-0000-0000-000000000002"


def _issue_rules(report: dict) -> list[str]:
    return [issue["rule"] for issue in report["issues"]]


def _banned(report: dict) -> list[dict]:
    return [i for i in report["issues"] if i["rule"] == "banned_word"]


def _taboo(report: dict) -> list[dict]:
    return [i for i in report["issues"] if i["rule"] == "persona_taboo"]


def test_banned_words_report_positions_and_are_errors():
    body = "全网最低价，100%有效"
    report = run_precheck(body, title="标题", platform="wechat", taboos=[])

    banned = _banned(report)
    assert {issue["word"] for issue in banned} == {"全网最低", "100%有效"}
    assert all(isinstance(issue["index"], int) for issue in banned)
    assert all(issue["field"] == "body" for issue in banned)
    for issue in banned:
        assert (
            body[issue["index"] : issue["index"] + len(issue["word"])]
            == issue["word"]
        )
    assert report["passed"] is False
    assert report["errors"] == 2
    assert report["warnings"] == 1
    assert [i["rule"] for i in report["issues"]].count("too_short") == 1


def test_banned_word_in_title_uses_title_relative_offset():
    title = "最佳标题"
    report = run_precheck(
        "这是正文。" * 70, title=title, platform="wechat", taboos=[]
    )

    banned = _banned(report)
    assert len(banned) == 1
    issue = banned[0]
    assert issue["field"] == "title"
    assert issue["word"] == "最佳"
    assert issue["index"] == 0
    assert title[issue["index"] : issue["index"] + len(issue["word"])] == "最佳"
    assert report["errors"] == 1
    assert report["warnings"] == 0


def test_clean_wechat_article_passes():
    report = run_precheck(
        "这是正文。" * 70, title="标题", platform="wechat", taboos=[]
    )

    assert report["passed"] is True
    assert report["errors"] == 0
    assert report["warnings"] == 0
    assert report["issues"] == []


def test_persona_taboo_warns_without_failing():
    body = "我们不做收益承诺，只讲方法。" + "这是正文。" * 20
    report = run_precheck(
        body, title="标题", platform="xiaohongshu", taboos=["收益承诺"]
    )

    assert report["passed"] is True
    assert report["errors"] == 0
    assert report["warnings"] == 1
    assert "too_short" not in _issue_rules(report)
    taboo_issues = _taboo(report)
    assert len(taboo_issues) == 1
    assert taboo_issues[0]["level"] == "warn"
    assert taboo_issues[0]["field"] == "body"
    assert taboo_issues[0]["index"] == body.find("收益承诺")


def test_short_wechat_article_warns():
    report = run_precheck("短文", title="标题", platform="wechat", taboos=[])

    assert report["passed"] is True
    assert report["errors"] == 0
    assert report["warnings"] == 1
    issue = report["issues"][0]
    assert (issue["level"], issue["rule"]) == ("warn", "too_short")
    assert "300" in issue["message"]


def test_missing_title_warns():
    report = run_precheck("这是正文。" * 70, platform="wechat", taboos=[])

    assert report["passed"] is True
    assert _issue_rules(report) == ["missing_title"]
    assert report["warnings"] == 1


def test_empty_text_is_error():
    report = run_precheck("  \n\t ", platform="wechat", taboos=[])

    assert report["passed"] is False
    assert report["errors"] == 1
    assert report["warnings"] == 0
    assert report["issues"] == [
        {"level": "error", "rule": "empty", "message": "正文为空"}
    ]


def test_too_long_is_error_not_warning():
    report = run_precheck(
        "正" * (MAX_LENGTH + 1), title="标题", platform="wechat", taboos=[]
    )

    assert report["passed"] is False
    assert report["errors"] == 1
    assert report["warnings"] == 0
    too_long = [i for i in report["issues"] if i["rule"] == "too_long"]
    assert len(too_long) == 1
    assert too_long[0]["level"] == "error"
    assert too_long[0]["message"] == f"正文超过 {MAX_LENGTH} 字"


def test_platform_specific_minimum_length():
    text_80 = "好" * 80

    xiaohongshu = run_precheck(
        text_80, title="标题", platform="xiaohongshu", taboos=[]
    )
    assert "too_short" not in _issue_rules(xiaohongshu)

    xiaohongshu_short = run_precheck(
        "好" * 79, title="标题", platform="xiaohongshu", taboos=[]
    )
    assert "too_short" in _issue_rules(xiaohongshu_short)

    zhihu = run_precheck(text_80, title="标题", platform="zhihu", taboos=[])
    zhihu_short = [i for i in zhihu["issues"] if i["rule"] == "too_short"]
    assert len(zhihu_short) == 1
    assert "200" in zhihu_short[0]["message"]

    unknown = run_precheck("好" * 5, title="标题", platform="douyin", taboos=[])
    assert "too_short" not in _issue_rules(unknown)


def test_fullwidth_banned_word_is_detected():
    report = run_precheck(
        "１００％有效", title="标题", platform="wechat", taboos=[]
    )

    banned = _banned(report)
    assert len(banned) == 1
    assert banned[0]["word"] == "100%有效"
    assert banned[0]["field"] == "body"
    assert report["passed"] is False
    assert report["errors"] == 1


def test_zero_width_banned_word_is_detected():
    report = run_precheck("最\u200b佳", title="标题", platform="wechat", taboos=[])

    banned = _banned(report)
    assert len(banned) == 1
    assert banned[0]["word"] == "最佳"
    assert banned[0]["field"] == "body"
    assert report["errors"] == 1


def test_multiple_banned_word_occurrences_each_reported():
    report = run_precheck(
        "保证保证", title="标题", platform="wechat", taboos=[]
    )

    banned = _banned(report)
    assert len(banned) == 2
    assert [issue["index"] for issue in banned] == [0, 2]
    assert all(issue["word"] == "保证" for issue in banned)
    assert report["errors"] == 2


async def _create_content(client, *, title="标题", body="正文", platform="wechat"):
    project = await client.post(
        "/api/projects", json={"title": f"检查测试-{uuid.uuid4().hex[:8]}"}
    )
    assert project.status_code == 201
    payload = {"platform": platform}
    if title:
        payload["title"] = title
    if body:
        payload["body"] = body
    content = await client.post(
        f"/api/projects/{project.json()['id']}/contents", json=payload
    )
    assert content.status_code == 201
    return content.json()


async def _create_job(client, content_id, *, platform="wechat"):
    payload = {"content_id": str(content_id), "platform": platform}
    if platform == "wechat":
        account = await client.post(
            "/api/accounts",
            json={
                "platform": "wechat",
                "name": f"检查账号-{uuid.uuid4().hex[:6]}",
                "credential": {"app_id": "pc-id", "app_secret": "pc-secret"},
            },
        )
        assert account.status_code == 201, account.text
        payload["account_id"] = account.json()["id"]
    response = await client.post("/api/publish/jobs", json=payload)
    assert response.status_code == 201
    return response.json()


async def _precheck(client, job_id):
    return await client.post(f"/api/publish/jobs/{job_id}/precheck")


async def _stored_job(db_session, job_id):
    return (
        await db_session.execute(
            select(PublishJob).where(PublishJob.id == uuid.UUID(str(job_id)))
        )
    ).scalar_one()


async def _events(db_session, job_id):
    return (
        await db_session.execute(
            select(PublishEvent).where(
                PublishEvent.job_id == uuid.UUID(str(job_id))
            )
        )
    ).scalars().all()


async def _project_of(db_session, content):
    return (
        await db_session.execute(
            select(Project).where(Project.id == uuid.UUID(content["project_id"]))
        )
    ).scalar_one()


async def _seed_foreign_job(db_session):
    project = Project(tenant_id=OTHER_TENANT, title="外部项目")
    db_session.add(project)
    await db_session.flush()
    content = Content(
        tenant_id=OTHER_TENANT,
        project_id=project.id,
        platform="wechat",
        title="外部内容",
        current_version=1,
    )
    db_session.add(content)
    await db_session.flush()
    db_session.add(
        ContentVersion(
            tenant_id=OTHER_TENANT,
            content_id=content.id,
            version=1,
            title="外部内容",
            body="外部正文",
        )
    )
    job = PublishJob(
        tenant_id=OTHER_TENANT,
        content_id=content.id,
        content_version=1,
        platform="wechat",
        status="draft",
        idempotency_key=f"foreign-{uuid.uuid4().hex}",
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


def _report_from(body: dict) -> dict:
    return {
        "passed": body["passed"],
        "errors": body["errors"],
        "warnings": body["warnings"],
        "issues": body["issues"],
    }


async def test_precheck_banned_content_fails_and_records_events(
    client, db_session
):
    content = await _create_content(
        client, title="标题", body="全网最低价，100%有效"
    )
    job = await _create_job(client, content["id"])

    response = await _precheck(client, job["id"])

    assert response.status_code == 200
    body = response.json()
    assert body["passed"] is False
    assert body["errors"] == 2
    assert body["warnings"] == 1
    assert body["job"]["status"] == "precheck_failed"
    assert body["job"]["checks"] == _report_from(body)

    db_session.expire_all()
    events = await _events(db_session, job["id"])
    assert len(events) == 3
    assert {event.action for event in events} == {"create", "submit", "fail"}
    by_action = {event.action: event for event in events}
    assert (by_action["submit"].from_status, by_action["submit"].to_status) == (
        "draft",
        "prechecking",
    )
    assert (by_action["fail"].from_status, by_action["fail"].to_status) == (
        "prechecking",
        "precheck_failed",
    )

    stored = await _stored_job(db_session, job["id"])
    assert stored.status == "precheck_failed"
    assert stored.checks == body["job"]["checks"]


async def test_precheck_clean_content_passes(client, db_session):
    content = await _create_content(
        client, title="标题", body="这是正文。" * 70
    )
    job = await _create_job(client, content["id"])

    response = await _precheck(client, job["id"])

    assert response.status_code == 200
    body = response.json()
    assert body["passed"] is True
    assert body["errors"] == 0
    assert body["job"]["status"] == "awaiting_review"
    assert body["job"]["checks"] == _report_from(body)

    db_session.expire_all()
    stored = await _stored_job(db_session, job["id"])
    assert stored.status == "awaiting_review"
    assert stored.checks == body["job"]["checks"]

    events = await _events(db_session, job["id"])
    assert len(events) == 3
    assert {event.action for event in events} == {"create", "submit", "pass"}
    by_action = {event.action: event for event in events}
    assert (by_action["pass"].from_status, by_action["pass"].to_status) == (
        "prechecking",
        "awaiting_review",
    )


async def test_recheck_from_precheck_failed_records_second_pair(
    client, db_session
):
    content = await _create_content(
        client, title="标题", body="全网最低价，100%有效"
    )
    job = await _create_job(client, content["id"])
    assert (await _precheck(client, job["id"])).status_code == 200

    second = await _precheck(client, job["id"])

    assert second.status_code == 200
    assert second.json()["passed"] is False
    assert second.json()["job"]["status"] == "precheck_failed"

    db_session.expire_all()
    events = await _events(db_session, job["id"])
    actions = [event.action for event in events]
    assert actions.count("create") == 1
    assert actions.count("submit") == 2
    assert actions.count("fail") == 2
    submits = sorted(
        (event.from_status, event.to_status)
        for event in events
        if event.action == "submit"
    )
    assert submits == [("draft", "prechecking"), ("precheck_failed", "prechecking")]


async def test_precheck_rescues_stuck_prechecking_status(client, db_session):
    content = await _create_content(
        client, title="标题", body="这是正文。" * 70
    )
    job = await _create_job(client, content["id"])
    stored = await _stored_job(db_session, job["id"])
    stored.status = "prechecking"
    await db_session.commit()

    response = await _precheck(client, job["id"])

    assert response.status_code == 200
    assert response.json()["job"]["status"] == "awaiting_review"

    db_session.expire_all()
    events = await _events(db_session, job["id"])
    assert [event.action for event in events].count("submit") == 0
    assert {event.action for event in events} == {"create", "pass"}
    pass_event = next(event for event in events if event.action == "pass")
    assert (
        pass_event.from_status,
        pass_event.to_status,
    ) == ("prechecking", "awaiting_review")


async def test_precheck_rejects_non_precheckable_status(client, db_session):
    content = await _create_content(
        client, title="标题", body="这是正文。" * 70
    )
    job = await _create_job(client, content["id"])
    stored = await _stored_job(db_session, job["id"])
    stored.status = "awaiting_review"
    await db_session.commit()

    response = await _precheck(client, job["id"])

    assert response.status_code == 409
    assert response.json()["code"] == "invalid_state"
    assert (
        response.json()["message"]
        == "只有草稿、检查未通过或检查中的任务可以执行检查"
    )
    db_session.expire_all()
    events = await _events(db_session, job["id"])
    assert [event.action for event in events] == ["create"]


async def test_precheck_missing_pinned_version_returns_409(client, db_session):
    content = await _create_content(client)
    job = await _create_job(client, content["id"])
    stored = await _stored_job(db_session, job["id"])
    stored.content_version = 99
    await db_session.commit()

    response = await _precheck(client, job["id"])

    assert response.status_code == 409
    assert response.json()["code"] == "empty_content"
    assert response.json()["message"] == "发布版本不存在"


async def test_precheck_unknown_or_foreign_job_returns_404(client, db_session):
    malformed = await _precheck(client, "not-a-uuid")
    assert malformed.status_code == 404
    assert malformed.json()["code"] == "not_found"

    foreign_job = await _seed_foreign_job(db_session)
    foreign = await _precheck(client, str(foreign_job.id))
    assert foreign.status_code == 404
    assert foreign.json()["code"] == "not_found"


async def test_precheck_job_pointing_at_foreign_content_returns_404(
    client, db_session
):
    content = await _create_content(
        client, title="标题", body="这是正文。" * 70
    )
    job = await _create_job(client, content["id"])
    foreign_job = await _seed_foreign_job(db_session)

    stored = await _stored_job(db_session, job["id"])
    stored.content_id = foreign_job.content_id
    await db_session.commit()

    response = await _precheck(client, job["id"])

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert response.json()["message"] == "内容不存在"


async def test_precheck_uses_project_profile_taboos(client, db_session):
    profile = Profile(
        tenant_id=DEFAULT_TENANT_ID,
        name=f"画像-{uuid.uuid4().hex[:8]}",
        taboos=["收益承诺"],
    )
    db_session.add(profile)
    await db_session.commit()
    await db_session.refresh(profile)

    content = await _create_content(
        client,
        title="标题",
        body="我们不做收益承诺，只讲方法。" + "这是正文。" * 20,
        platform="xiaohongshu",
    )
    project = await _project_of(db_session, content)
    project.profile_id = profile.id
    await db_session.commit()

    job = await _create_job(client, content["id"], platform="xiaohongshu")
    response = await _precheck(client, job["id"])

    assert response.status_code == 200
    body = response.json()
    assert body["passed"] is True
    assert body["warnings"] == 1
    taboo_issues = _taboo(_report_from(body))
    assert len(taboo_issues) == 1
    assert taboo_issues[0]["field"] == "body"


async def test_foreign_profile_taboos_do_not_leak(client, db_session):
    foreign_profile = Profile(
        tenant_id=OTHER_TENANT,
        name="外部画像",
        taboos=["收益承诺"],
    )
    db_session.add(foreign_profile)
    await db_session.commit()
    await db_session.refresh(foreign_profile)

    content = await _create_content(
        client,
        title="标题",
        body="我们不做收益承诺，只讲方法。" + "这是正文。" * 20,
        platform="xiaohongshu",
    )
    project = await _project_of(db_session, content)
    project.profile_id = foreign_profile.id
    await db_session.commit()

    job = await _create_job(client, content["id"], platform="xiaohongshu")
    response = await _precheck(client, job["id"])

    assert response.status_code == 200
    body = response.json()
    assert body["passed"] is True
    assert body["warnings"] == 0
    assert "persona_taboo" not in _issue_rules(_report_from(body))


async def test_precheck_uses_pinned_version_not_latest(client, db_session):
    content = await _create_content(
        client, title="标题", body="这是正文。" * 70
    )
    job = await _create_job(client, content["id"])

    stored_content = (
        await db_session.execute(
            select(Content).where(Content.id == uuid.UUID(content["id"]))
        )
    ).scalar_one()
    stored_content.current_version = 2
    db_session.add(
        ContentVersion(
            tenant_id=DEFAULT_TENANT_ID,
            content_id=stored_content.id,
            version=2,
            title="标题",
            body="全网最低价，100%有效",
        )
    )
    await db_session.commit()

    response = await _precheck(client, job["id"])

    assert response.status_code == 200
    body = response.json()
    assert body["passed"] is True
    assert body["errors"] == 0
    assert body["job"]["status"] == "awaiting_review"
    assert not _banned(_report_from(body))

    db_session.expire_all()
    stored = await _stored_job(db_session, job["id"])
    assert stored.content_version == 1
    assert stored.checks == body["job"]["checks"]
    assert not _banned(stored.checks)
