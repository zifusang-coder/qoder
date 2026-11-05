import json
import sys
from pathlib import Path

SHARED = Path(__file__).resolve().parents[2] / "shared" / "scripts"
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

from tts_common import (  # noqa: E402
    MAX_TEXT_CHARS,
    TTSError,
    merge_srt,
    normalize_rate,
    normalize_voice,
    scene_srt,
    synthesize,
)

ROOT = Path(__file__).resolve().parents[3]
OUTPUTS_ROOT = (ROOT / "outputs").resolve()
MAX_TITLE_CHARS = 40


def normalize(text: object) -> str:
    return " ".join(str(text or "").split())


def resolve_output_dir(raw: str) -> Path:
    candidate = Path(raw)
    target = (
        candidate.resolve() if candidate.is_absolute() else (ROOT / candidate).resolve()
    )
    try:
        target.relative_to(OUTPUTS_ROOT)
    except ValueError:
        raise SystemExit("output_dir 必须位于 outputs 目录下") from None
    return target


def main() -> None:
    payload = json.load(sys.stdin)
    inputs = payload.get("inputs", {})
    output = payload.get("output") or {}
    if not isinstance(inputs, dict) or not isinstance(output, dict):
        raise SystemExit("payload 结构不正确")

    raw_dir = normalize(inputs.get("output_dir"))
    if not raw_dir:
        raise SystemExit("output_dir 不能为空")
    out_dir = resolve_output_dir(raw_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    narration = normalize(output.get("narration")) or normalize(inputs.get("text"))
    if not narration:
        raise SystemExit("口播稿不能为空")
    narration = narration[:MAX_TEXT_CHARS]
    title = (normalize(inputs.get("title")) or normalize(output.get("title")))[
        :MAX_TITLE_CHARS
    ]
    voice_name = normalize_voice(inputs.get("voice_name"))
    rate = normalize_rate(inputs.get("rate"))

    audio_path = out_dir / "voiceover.mp3"
    try:
        result = synthesize(
            narration,
            audio_path,
            voice=voice_name,
            rate=rate,
            receive_timeout=90,
        )
    except TTSError as exc:
        raise SystemExit(str(exc)) from exc

    subtitles_path = out_dir / "subtitles.srt"
    srt = merge_srt([result.srt]) if result.srt else scene_srt(
        [narration], [result.duration]
    )
    subtitles_path.write_text(srt, encoding="utf-8")

    script_path = out_dir / "script.txt"
    script_text = "\n".join(part for part in (title, narration) if part)
    script_path.write_text(script_text + "\n", encoding="utf-8")

    files = [
        {"name": "voiceover.mp3", "path": str(audio_path), "kind": "audio"},
        {"name": "subtitles.srt", "path": str(subtitles_path), "kind": "subtitles"},
        {"name": "script.txt", "path": str(script_path), "kind": "text"},
    ]
    print(
        json.dumps(
            {
                "kind": "audio",
                "title": title,
                "narration": narration,
                "voice": voice_name,
                "rate": rate,
                "duration_seconds": round(result.duration, 3),
                "files": files,
                "preview": "voiceover.mp3",
                "subtitles": "subtitles.srt",
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
