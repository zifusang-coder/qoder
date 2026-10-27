from dataclasses import dataclass, field

# 规则按“先具体、后泛化”排列：具体动作（改写/风险/复盘等）优先于
# 平台或体裁名词（公众号/小红书/笔记/标题），避免被泛词抢先命中。
RULES: list[tuple[str, tuple[str, ...]]] = [
    ("content-repurposing", ("改写", "一稿多改", "改成", "转成")),
    ("cover-card", ("封面",)),
    ("risk-scan", ("风险", "合规", "违规")),
    ("postmortem", ("复盘", "数据分析")),
    ("title-hook", ("标题",)),
    ("topic-evaluator", ("选题", "评估")),
    ("article-outline", ("大纲", "提纲")),
    ("wechat-article", ("公众号文章", "公众号长文", "写公众号", "微信文章")),
    ("xhs-note", ("小红书", "笔记")),
    ("copywriting", ("口播", "短视频文案", "抖音文案", "知乎回答")),
]

PLATFORM_HINTS = {
    "小红书": "xiaohongshu",
    "公众号": "wechat",
    "抖音": "douyin",
    "快手": "kuaishou",
    "视频号": "channels",
    "知乎": "zhihu",
    "b站": "bilibili",
    "B站": "bilibili",
    "微信": "wechat",
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


def _skill_inputs(skill_id: str, text: str, platform: str, output_dir: str) -> dict:
    if skill_id == "content-repurposing":
        return {"source": text, "platform": platform}
    if skill_id == "cover-card":
        return {"title": text, "output_dir": output_dir}
    if skill_id == "risk-scan":
        return {"text": text, "platform": platform}
    if skill_id == "postmortem":
        return {"platform": platform}
    if skill_id == "wechat-article":
        return {"topic": text, "outline": text}
    if skill_id == "copywriting":
        return {"topic": text, "platform": platform}
    if skill_id == "xhs-note":
        return {"topic": text}
    return {"topic": text, "platform": platform}


def route_intent(message: str, *, output_dir: str = "outputs/adhoc") -> Intent:
    text = message.strip()
    for skill_id, keywords in RULES:
        if any(word in text for word in keywords):
            platform = detect_platform(text)
            if skill_id == "copywriting" and platform not in {
                "douyin", "channels", "zhihu", "kuaishou", "bilibili",
            }:
                platform = "douyin"
            inputs = _skill_inputs(skill_id, text, platform, output_dir)
            return Intent(kind="skill", skill_id=skill_id, inputs=inputs)
    return Intent(kind="chat")
