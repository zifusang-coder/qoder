import json
import re
import sys
from pathlib import Path

MAX_HITS = 100


def main() -> None:
    payload = json.load(sys.stdin)
    inputs = payload.get("inputs") or {}
    output = payload.get("output") or {}
    text = str(inputs.get("text", ""))

    words = [
        w.strip()
        for w in (Path(__file__).resolve().parent.parent / "references" / "words.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if w.strip() and not w.startswith("#")
    ]
    hits = []
    total = 0
    for word in words:
        for match in re.finditer(re.escape(word), text):
            total += 1
            if len(hits) >= MAX_HITS:
                continue
            start = match.start()
            hits.append(
                {
                    "word": word,
                    "index": start,
                    "context": text[max(0, start - 10) : start + len(word) + 10],
                }
            )
    hits.sort(key=lambda hit: (hit["index"], hit["word"]))

    level = "high" if total >= 2 else "medium" if total else "low"
    merged = dict(output) if isinstance(output, dict) else {}
    merged["summary"] = level
    merged["scan"] = {
        "risk_level": level,
        "hits": hits,
        "total": total,
        "truncated": total > len(hits),
    }
    print(json.dumps(merged, ensure_ascii=False))


if __name__ == "__main__":
    main()
