import json
import sys
from pathlib import Path


def main() -> None:
    payload = json.load(sys.stdin)
    out = Path(payload["inputs"]["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    files = [
        ("voiceover.mp3", b"fake-mp3", "audio"),
        ("subtitles.srt", b"1\n00:00:00,000 --> 00:00:02,000\nhello\n", "subtitles"),
        ("script.txt", b"script\n", "text"),
    ]
    written = []
    for name, content, kind in files:
        path = out / name
        path.write_bytes(content)
        written.append({"name": name, "path": str(path), "kind": kind})
    print(
        json.dumps(
            {
                "kind": "audio",
                "title": "测试配音",
                "narration": "测试口播",
                "voice": "zh-CN-XiaoxiaoNeural",
                "rate": "+0%",
                "duration_seconds": 2.5,
                "files": written,
                "preview": "voiceover.mp3",
                "subtitles": "subtitles.srt",
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
