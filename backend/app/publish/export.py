import io
import json
import zipfile
from datetime import UTC, datetime

from app.storage import Storage

GUIDES: dict[str, list[str]] = {
    "xiaohongshu": [
        "打开小红书 App，点击「+」创建新笔记",
        "粘贴标题与正文，上传正文配图（如有）",
        "使用项目素材中的封面，调整裁剪比例",
        "添加话题标签后发布",
    ],
    "zhihu": [
        "打开知乎，选择「写文章」",
        "粘贴标题和正文，检查段落与引用格式",
        "上传正文配图（如有），使用项目素材中的封面",
        "添加话题后发布",
    ],
    "kuaishou": [
        "打开快手 App，点击「+」创建作品",
        "粘贴标题与文案，上传正文配图（如有）",
        "使用项目素材中的封面设置作品封面",
        "选择话题并发布",
    ],
    "channels": [
        "打开微信视频号助手，进入「发表内容」",
        "粘贴标题与正文，上传正文配图（如有）",
        "使用项目素材中的封面设置封面",
        "确认可见范围后发表",
    ],
}

MANUAL_EXPORT_PLATFORMS = frozenset(GUIDES)


def _guide_markdown(steps: list[str]) -> str:
    lines = ["# 发布步骤", ""]
    lines.extend(f"{index}. {step}" for index, step in enumerate(steps, start=1))
    return "\n".join(lines) + "\n"


class ExportAdapter:
    capability = "manual_export"

    def __init__(self, storage: Storage):
        self.storage = storage

    def export(
        self,
        *,
        project_id: str,
        platform: str,
        job_id: str,
        content_id: str,
        content_version: int,
        title: str,
        body: str,
    ) -> str:
        exported_at = datetime.now(UTC)
        checklist = GUIDES[platform]
        manifest = {
            "platform": platform,
            "title": title,
            "content_id": content_id,
            "job_id": job_id,
            "content_version": content_version,
            "exported_at": exported_at.isoformat().replace("+00:00", "Z"),
            "checklist": checklist,
        }

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
            archive.writestr("content.md", f"# {title}\n\n{body}\n")
            archive.writestr("publish_guide.md", _guide_markdown(checklist))

        job_hex = str(job_id).replace("-", "")
        relpath = f"exports/{platform}-{job_hex}-v{content_version}.zip"
        return self.storage.save_bytes(project_id, relpath, buffer.getvalue())
