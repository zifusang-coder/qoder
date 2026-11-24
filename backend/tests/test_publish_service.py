import uuid


async def _content(client, *, platform, kind):
    project = await client.post(
        "/api/projects", json={"title": f"服务-{uuid.uuid4().hex[:8]}"}
    )
    response = await client.post(
        f"/api/projects/{project.json()['id']}/contents",
        json={"platform": platform, "kind": kind, "title": "标题", "body": "正文"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _approved_job(client, content, *, account_id=None):
    payload = {"content_id": content["id"], "platform": content["platform"]}
    if account_id:
        payload["account_id"] = account_id
    created = await client.post("/api/publish/jobs", json=payload)
    assert created.status_code == 201, created.text
    job = created.json()
    precheck = await client.post(
        f"/api/publish/jobs/{job['id']}/precheck"
    )
    assert precheck.status_code == 200, precheck.text
    approve = await client.post(
        f"/api/publish/jobs/{job['id']}/actions",
        json={"action": "approve"},
    )
    assert approve.status_code == 200, approve.text
    return job


async def test_pending_platform_execute_returns_409(client):
    content = await _content(client, platform="toutiao", kind="article")
    job = await _approved_job(client, content)

    response = await client.post(
        f"/api/publish/jobs/{job['id']}/execute"
    )

    assert response.status_code == 409
    assert response.json()["code"] == "platform_not_available"
    assert "图文接口" in response.json()["message"]


async def test_manual_platform_execute_returns_409(client):
    content = await _content(client, platform="xiaohongshu", kind="note")
    job = await _approved_job(client, content)

    response = await client.post(
        f"/api/publish/jobs/{job['id']}/execute"
    )

    assert response.status_code == 409
    assert response.json()["code"] == "manual_export_required"


async def test_manual_export_still_works(client):
    content = await _content(client, platform="xiaohongshu", kind="note")
    job = await _approved_job(client, content)

    response = await client.post(
        f"/api/publish/jobs/{job['id']}/export"
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "exported"
