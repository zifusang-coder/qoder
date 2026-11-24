import json
import sys
from pathlib import Path


def main() -> None:
    payload = json.load(sys.stdin)
    out = Path(payload["inputs"]["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    files = [
        ("video.mp4", b"fake-mp4", "video"),
        ("cover.png", b"fake-png", "image"),
        ("subtitles.srt", b"1\n00:00:00,000 --> 00:00:04,000\nhello\n", "subtitles"),
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
                "kind": "video",
                "title": "测试短视频",
                "narration": "测试口播",
                "hashtags": [],
                "duration_seconds": 8,
                "audio": {"status": "disabled"},
                "files": written,
                "preview": "video.mp4",
                "cover": "cover.png",
                "subtitles": "subtitles.srt",
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
