import re
import unicodedata

from app.publish.rules import BANNED_WORDS, MAX_LENGTH, MIN_LENGTH

_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200d\ufeff]")


def _issue(level: str, rule: str, message: str, **extra) -> dict:
    return {"level": level, "rule": rule, "message": message, **extra}


def _normalize(value: str) -> str:
    return _ZERO_WIDTH_RE.sub("", unicodedata.normalize("NFKC", value))


def run_precheck(
    body: str, *, platform: str, taboos: list[str], title: str = ""
) -> dict:
    issues: list[dict] = []

    if not body or not body.strip():
        issues.append(_issue("error", "empty", "正文为空"))
        return _report(issues)

    if not title.strip():
        issues.append(_issue("warn", "missing_title", "缺少一级标题"))

    body_len = len(re.sub(r"\s+", "", body))
    min_len = MIN_LENGTH.get(platform, 0)
    if body_len < min_len:
        issues.append(
            _issue(
                "warn",
                "too_short",
                f"{platform} 建议不少于 {min_len} 字，当前 {body_len} 字",
            )
        )
    if body_len > MAX_LENGTH:
        issues.append(_issue("error", "too_long", f"正文超过 {MAX_LENGTH} 字"))

    for field, text in (("title", title), ("body", body)):
        normalized = _normalize(text)
        for word in BANNED_WORDS:
            for match in re.finditer(re.escape(_normalize(word)), normalized):
                issues.append(
                    _issue(
                        "error",
                        "banned_word",
                        f"命中广告法或平台高风险词「{word}」",
                        field=field,
                        index=match.start(),
                        word=word,
                    )
                )
        for taboo in taboos:
            if not isinstance(taboo, str) or not taboo:
                continue
            needle = _normalize(taboo)
            if not needle:
                continue
            for match in re.finditer(re.escape(needle), normalized):
                issues.append(
                    _issue(
                        "warn",
                        "persona_taboo",
                        f"触及账号禁区「{taboo}」，请确认表达",
                        field=field,
                        index=match.start(),
                        word=taboo,
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
