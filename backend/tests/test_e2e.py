import json
import uuid
import zipfile
from pathlib import Path

from app.skills.registry import SkillRegistry, get_registry
from app.storage import Storage, get_storage

FIXTURE = Path(__file__).parent / "fixtures" / "skills"
PARAGRAPH = "把一件事讲清楚，比堆砌概念更重要。"
# 正文需超过平台最短长度阈值，避免发布前检查出现 too_short 警告。
CLEAN_BODY = "# 一稿多改实战\n\n" + PARAGRAPH * 30
REPURPOSED_BODY = "# 小红书版\n\n" + PARAGRAPH * 30
POSTMORTEM = {
    "verdict": "超预期",
    "what_worked": ["开头具体"],
    "what_failed": [],
    "next_actions": ["保持"],
    "memories": [
        {"kind": "insight", "content": "场景化开头有效", "weight": 0.9}
    ],
}
CSV_HEADER = (
    "content_id,platform,captured_at,views,likes,comments,shares,saves,"
    "followers_delta"
)


class E2EGateway:
    def __init__(self):
        self.calls: list[dict] = []

    async def complete(self, messages, **kwargs):
        inputs = json.loads(messages[-1]["content"])
        self.calls.append(inputs)
        if "metrics" in inputs:
            return json.dumps(POSTMORTEM, ensure_ascii=False)
        if "topic" in inputs:
            return REPURPOSED_BODY
        raise AssertionError(f"未预期的 LLM 输入: {sorted(inputs)}")


async def test_full_mvp_flow(client, tmp_path, monkeypatch):
    from app.main import app

    gateway = E2EGateway()

    async def fake_gateway(session, tenant_id):
        return gateway

    try:
        app.dependency_overrides[get_registry] = lambda: SkillRegistry(FIXTURE)
        app.dependency_overrides[get_storage] = lambda: Storage(tmp_path)
        # 复盘与一稿多改在服务内部延迟加载网关，不经过 FastAPI 依赖覆写
        monkeypatch.setattr(
            "app.services.repurpose.load_gateway_default", fake_gateway
        )
        monkeypatch.setattr(
            "app.services.postmortem.load_gateway_default", fake_gateway
        )

        profile_response = await client.post(
            "/api/profiles",
            json={"name": "主号", "taboos": ["收益承诺"]},
        )
        assert profile_response.status_code == 201
        profile = profile_response.json()
        assert profile["taboos"] == ["收益承诺"]

        project_response = await client.post(
            "/api/projects",
            json={"title": "Phase 1 闭环", "profile_id": profile["id"]},
        )
        assert project_response.status_code == 201
        project = project_response.json()

        content_response = await client.post(
            f"/api/projects/{project['id']}/contents",
            json={
                "platform": "wechat",
                "title": "一稿多改实战",
                "body": CLEAN_BODY,
            },
        )
        assert content_response.status_code == 201
        content = content_response.json()
        assert content["current_version"] == 1

        # 一稿多改：为目标平台生成独立内容与版本
        repurpose_response = await client.post(
            f"/api/projects/{project['id']}/repurpose",
            json={
                "source_content_id": content["id"],
                "platform": "xiaohongshu",
                "skill_id": "demo-skill",
            },
        )
        assert repurpose_response.status_code == 201
        repurposed = repurpose_response.json()
        assert repurposed["platform"] == "xiaohongshu"
        assert repurposed["version"] == 1
        assert repurposed["body"] == REPURPOSED_BODY

        contents = (
            await client.get(f"/api/projects/{project['id']}/contents")
        ).json()
        assert len(contents) == 2
        xiaohongshu = next(
            item for item in contents if item["platform"] == "xiaohongshu"
        )
        assert xiaohongshu["id"] == repurposed["content_id"]
        assert xiaohongshu["id"] != content["id"]
        assert xiaohongshu["current_version"] == 1

        # 发布：创建任务 → 发布前检查 → 人工审核 → 导出
        job_response = await client.post(
            "/api/publish/jobs",
            json={
                "content_id": xiaohongshu["id"],
                "platform": "xiaohongshu",
            },
        )
        assert job_response.status_code == 201
        job = job_response.json()
        assert job["status"] == "draft"
        assert job["content_version"] == 1

        precheck_response = await client.post(
            f"/api/publish/jobs/{job['id']}/precheck"
        )
        assert precheck_response.status_code == 200
        precheck = precheck_response.json()
        assert precheck["passed"] is True
        assert precheck["errors"] == 0
        assert precheck["warnings"] == 0
        assert precheck["job"]["status"] == "awaiting_review"

        approved_response = await client.post(
            f"/api/publish/jobs/{job['id']}/actions",
            json={"action": "approve"},
        )
        assert approved_response.status_code == 200
        assert approved_response.json()["status"] == "approved"

        export_response = await client.post(
            f"/api/publish/jobs/{job['id']}/export"
        )
        assert export_response.status_code == 200
        exported = export_response.json()
        assert exported["status"] == "exported"
        assert exported["export_path"].startswith("exports/xiaohongshu-")
        assert exported["export_path"].endswith("-v1.zip")

        archive_path = tmp_path / project["id"] / exported["export_path"]
        assert archive_path.is_file()
        with zipfile.ZipFile(archive_path) as archive:
            assert sorted(archive.namelist()) == [
                "content.md",
                "manifest.json",
                "publish_guide.md",
            ]
            manifest = json.loads(
                archive.read("manifest.json").decode("utf-8")
            )
            exported_body = archive.read("content.md").decode("utf-8")
        assert manifest["content_id"] == xiaohongshu["id"]
        assert manifest["job_id"] == job["id"]
        assert manifest["content_version"] == 1
        assert REPURPOSED_BODY in exported_body

        # 数据导入：重复导入同一天应 upsert，不产生重复行
        csv_body = (
            CSV_HEADER
            + "\n"
            + f"{xiaohongshu['id']},xiaohongshu,2026-10-22,"
            "2000,120,18,25,10,7\n"
        ).encode("utf-8")
        for _ in range(2):
            imported_response = await client.post(
                "/api/analytics/import",
                files={"file": ("analytics.csv", csv_body, "text/csv")},
            )
            assert imported_response.status_code == 200
            assert imported_response.json() == {"upserted": 1, "errors": []}

        overview_response = await client.get(
            "/api/analytics/overview", params={"project_id": project["id"]}
        )
        assert overview_response.status_code == 200
        overview = overview_response.json()
        assert overview["totals"]["views"] == 2000
        rows = [
            item
            for item in overview["items"]
            if item["content_id"] == xiaohongshu["id"]
        ]
        assert len(rows) == 1
        assert rows[0]["views"] == 2000

        # 复盘：生成报告并把经验回写画像记忆
        postmortem_response = await client.post(
            f"/api/analytics/contents/{xiaohongshu['id']}/postmortem",
            json={"skill_id": "postmortem-fake"},
        )
        assert postmortem_response.status_code == 200
        result = postmortem_response.json()
        assert result["report"]["verdict"] == POSTMORTEM["verdict"]
        assert len(result["memory_ids"]) == 1
        assert uuid.UUID(result["run_id"])

        memories = (
            await client.get(f"/api/profiles/{profile['id']}/memories")
        ).json()
        generated = [
            memory for memory in memories if memory["source"] == "postmortem"
        ]
        assert len(generated) == 1
        assert generated[0]["id"] == result["memory_ids"][0]
        assert generated[0]["content"] == "场景化开头有效"
        assert generated[0]["kind"] == "insight"
        assert generated[0]["weight"] == 0.9

        # 版本回溯：新建版本后按版本号倒序返回全部历史
        second_response = await client.post(
            f"/api/contents/{xiaohongshu['id']}/versions",
            json={
                "title": "小红书版二稿",
                "body": REPURPOSED_BODY + "\n\n补充一段二稿说明。",
            },
        )
        assert second_response.status_code == 201
        assert second_response.json()["version"] == 2

        versions_response = await client.get(
            f"/api/contents/{xiaohongshu['id']}/versions"
        )
        assert versions_response.status_code == 200
        versions = versions_response.json()["items"]
        assert [item["version"] for item in versions] == [2, 1]
        assert versions[0]["title"] == "小红书版二稿"
        assert versions[1]["body"] == REPURPOSED_BODY
        assert versions[1]["source"] == "skill"

        contents_after = (
            await client.get(f"/api/projects/{project['id']}/contents")
        ).json()
        updated = next(
            item for item in contents_after if item["id"] == xiaohongshu["id"]
        )
        assert updated["current_version"] == 2
        assert len(gateway.calls) == 2
        assert "metrics" in gateway.calls[1]
    finally:
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_storage, None)
