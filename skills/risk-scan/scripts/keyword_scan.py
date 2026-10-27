import json
import sys
from pathlib import Path


def main() -> None:
    payload = json.load(sys.stdin)
    inputs = payload.get("inputs") or {}
    output = payload.get("output") or {}
    parts = [str(inputs.get("text", ""))]
    if isinstance(output, dict):
        for key, value in output.items():
            if key == "reviewed_points":
                continue
            if isinstance(value, str):
                parts.append(value)
        points = output.get("reviewed_points") or []
        if isinstance(points, list):
            parts.extend(str(item) for item in points if isinstance(item, str))
    text = " ".join(part for part in parts if part)

    words = [
        w.strip()
        for w in (Path(__file__).resolve().parent.parent / "references" / "words.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if w.strip() and not w.startswith("#")
    ]
    hits = []
    for word in words:
        start = text.find(word)
        if start >= 0:
            hits.append(
                {
                    "word": word,
                    "index": start,
                    "context": text[max(0, start - 10) : start + len(word) + 10],
                }
            )

    level = "high" if len(hits) >= 2 else "medium" if hits else "low"
    merged = dict(output) if isinstance(output, dict) else {"text": text}
    merged["scan"] = {"risk_level": level, "hits": hits, "total": len(hits)}
    print(json.dumps(merged, ensure_ascii=False))


if __name__ == "__main__":
    main()
