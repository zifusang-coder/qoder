import re

from app.publish.rules import BANNED_WORDS, MAX_LENGTH, MIN_LENGTH

_TITLE_RE = re.compile(r"^\s*#\s+\S+", re.MULTILINE)


def _issue(level: str, rule: str, message: str, **extra) -> dict:
    return {"level": level, "rule": rule, "message": message, **extra}


def run_precheck(text: str, *, platform: str, taboos: list[str]) -> dict:
    issues: list[dict] = []

    if not text or not text.strip():
        issues.append(_issue("error", "empty", "正文为空"))
        return _report(issues)

    if _TITLE_RE.search(text) is None:
        issues.append(_issue("warn", "missing_title", "缺少一级标题"))

    body_len = len(re.sub(r"\s+", "", text))
    min_len = MIN_LENGTH.get(platform, 0)
    if body_len < min_len:
        issues.append(
            _issue(
                "warn",
                "too_short",
                f"{platform} 建议不少于 {min_len} 字，当前 {body_len} 字",
            )
        )
    if len(text) > MAX_LENGTH:
        issues.append(_issue("error", "too_long", f"正文超过 {MAX_LENGTH} 字符"))

    for word in BANNED_WORDS:
        index = text.find(word)
        if index >= 0:
            issues.append(
                _issue(
                    "error",
                    "banned_word",
                    f"命中广告法或平台高风险词「{word}」",
                    index=index,
                    word=word,
                )
            )

    for taboo in taboos:
        if not isinstance(taboo, str) or not taboo:
            continue
        index = text.find(taboo)
        if index >= 0:
            issues.append(
                _issue(
                    "warn",
                    "persona_taboo",
                    f"触及账号禁区「{taboo}」，请确认表达",
                    index=index,
                )
            )

    return _report(issues)


def _report(issues: list[dict]) -> dict:
    errors = sum(1 for issue in issues if issue["level"] == "error")
    warnings = sum(1 for issue in issues if issue["level"] == "warn")
    return {
        "passed": errors == 0,
        "errors": errors,
        "warnings": warnings,
        "issues": issues,
    }
