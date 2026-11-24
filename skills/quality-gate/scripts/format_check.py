import json
import sys

BANNED = ["震惊", "必看", "稳赚", "包治", "国家级", "第一品牌"]
VAGUE = ["赋能", "闭环", "抓手", "打法", "底层逻辑"]


def main() -> None:
    payload = json.load(sys.stdin)
    text = payload.get("inputs", {}).get("text", "")
    base = dict(payload.get("output") or {})

    issues = []
    lines = text.splitlines()
    nonempty = [ln for ln in lines if ln.strip()]
    if not nonempty:
        issues.append({"level": "error", "message": "文本为空"})
    if nonempty and not nonempty[0].lstrip().startswith("#"):
        issues.append({"level": "warn", "message": "缺少一级标题"})

    for line_no, para in enumerate(lines, start=1):
        if not para.strip() or para.lstrip().startswith("#"):
            continue
        if len(para) > 180:
            issues.append(
                {"level": "warn", "message": f"第 {line_no} 行正文段落超过 180 字"}
            )
            break

    for word in BANNED:
        if word in text:
            issues.append(
                {
                    "level": "error",
                    "message": f"命中禁用词「{word}」",
                    "index": text.index(word),
                }
            )
    for word in VAGUE:
        if word in text:
            issues.append(
                {
                    "level": "warn",
                    "message": f"空泛表达「{word}」",
                    "index": text.index(word),
                }
            )

    base["check"] = {
        "passed": not any(i["level"] == "error" for i in issues),
        "issues": issues,
    }
    print(json.dumps(base, ensure_ascii=False))


if __name__ == "__main__":
    main()
