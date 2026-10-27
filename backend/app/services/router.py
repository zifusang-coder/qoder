from dataclasses import dataclass, field

RULES: list[tuple[str, tuple[str, ...]]] = [
    ("topic-evaluator", ("选题", "评估")),
    ("article-outline", ("大纲", "提纲")),
    ("wechat-article", ("公众号", "长文")),
    ("xhs-note", ("小红书", "笔记")),
    ("title-hook", ("标题",)),
    ("copywriting", ("口播", "短视频文案", "抖音", "知乎")),
    ("cover-card", ("封面",)),
    ("risk-scan", ("风险", "合规", "违规")),
    ("content-repurposing", ("改写", "一稿多改")),
    ("postmortem", ("复盘", "数据分析")),
]

PLATFORM_HINTS = {
    "小红书": "xiaohongshu",
    "公众号": "wechat",
    "抖音": "douyin",
    "快手": "kuaishou",
    "视频号": "channels",
    "知乎": "zhihu",
    "B站": "bilibili",
}


@dataclass
class Intent:
    kind: str
    skill_id: str | None = None
    inputs: dict = field(default_factory=dict)


def detect_platform(message: str) -> str:
    for hint, platform in PLATFORM_HINTS.items():
        if hint in message:
            return platform
    return "wechat"


def route_intent(message: str, *, output_dir: str = "outputs/adhoc") -> Intent:
    text = message.strip()
    for skill_id, keywords in RULES:
        if any(word in text for word in keywords):
            platform = detect_platform(text)
            if skill_id == "copywriting" and platform not in {
                "douyin", "channels", "zhihu", "kuaishou", "bilibili",
            }:
                platform = "douyin"
            inputs: dict = {
                "topic": text,
                "text": text,
                "source": text,
                "outline": text,
                "platform": platform,
            }
            if skill_id == "cover-card":
                inputs["title"] = text
                inputs["output_dir"] = output_dir
            return Intent(kind="skill", skill_id=skill_id, inputs=inputs)
    return Intent(kind="chat")
